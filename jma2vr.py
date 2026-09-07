# -*- coding: utf-8 -*-

import asyncio
import datetime
import json
from collections import OrderedDict, deque

import requests
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

# ============================================================
# Configuration
# ============================================================

VR_URL = "http://127.0.0.1:4532/talk"
WS_URL = "wss://ws-api.wolfx.jp/jma_eew"

USER_AGENT = "jma2vr/2.1"

# WebSocket 断线后等待多久重连
RECONNECT_DELAY = 3

# SimpleVoiceroid2Proxy 暂时不可用时的重试间隔
VOICE_RETRY_DELAY = 3

# 保存最近多少个 EventID + Serial，防止重连/query 造成重复处理
SEEN_REPORT_LIMIT = 2048

# 保存最近多少个不同地震事件的朗读文本
EVENT_TEXT_CACHE_LIMIT = 256


# ============================================================
# Runtime state
# ============================================================

# 等待发送给 SimpleVoiceroid2Proxy 的文本
voice_queue = asyncio.Queue()

# requests 连接复用
http_session = requests.Session()

# 已经处理过的 EventID + Serial
seen_reports = set()
seen_report_order = deque()

# 每个地震事件最后一次加入朗读队列的文本
#
# 用于实现：
#
# 第3报:
#   宮城県沖 / M5.2 / 最大震度4
#
# 第4报:
#   宮城県沖 / M5.2 / 最大震度4
#
# → 第4报不重复朗读
#
# 但另一个 EventID 即使内容碰巧完全相同，仍然正常朗读。
last_text_by_event = OrderedDict()

# 是否已经取得过启动时的当前 EEW
initialized = False


# ============================================================
# Utilities
# ============================================================


def log(message):
    print(f"{datetime.datetime.now().strftime('[%Y-%m-%d %H:%M:%S]')} " f"{message}")


def mark_report_seen(event_id, serial):
    """
    使用 EventID + Serial 判断同一份 EEW 报文是否已经处理。

    主要防止：
      - WebSocket 重复推送
      - 断线重连后的 query_jmaeew
      - 网络异常情况下收到同一报多次
    """

    report_id = (event_id, serial)

    if report_id in seen_reports:
        return False

    seen_reports.add(report_id)
    seen_report_order.append(report_id)

    # 防止集合无限增长
    while len(seen_report_order) > SEEN_REPORT_LIMIT:
        old_report = seen_report_order.popleft()
        seen_reports.discard(old_report)

    return True


def get_last_event_text(event_id):
    """
    取得该 EventID 上一次准备朗读的文本。
    """

    text = last_text_by_event.get(event_id)

    if text is not None:
        last_text_by_event.move_to_end(event_id)

    return text


def set_last_event_text(event_id, text):
    """
    保存该 EventID 最新一次准备朗读的文本。
    """

    last_text_by_event[event_id] = text
    last_text_by_event.move_to_end(event_id)

    # 防止长时间运行后缓存无限增长
    while len(last_text_by_event) > EVENT_TEXT_CACHE_LIMIT:
        last_text_by_event.popitem(last=False)


# ============================================================
# Voice text
# ============================================================


def build_voice_text(data):
    """
    根据 Wolfx JMA EEW JSON 生成实际交给 VOICEROID2 的文本。

    注意：
    不把 Serial 放进朗读文本。

    这样即使：
        第3报 → M5.2 / 震度4
        第4报 → M5.2 / 震度4

    两次真正需要朗读的文本仍完全相同，
    可以识别并跳过第4报。
    """

    if data.get("isCancel"):
        return "緊急地震速報は取り消されました。"

    hypocenter = data.get("Hypocenter", "不明")
    magnitude = data.get("Magnitude", "不明")
    max_intensity = data.get("MaxIntensity", "不明")

    return (
        f"{hypocenter}で地震。\n"
        f"マグニチュード{magnitude}。\n"
        f"推定された最大震度は、{max_intensity}です。"
    )


# ============================================================
# SimpleVoiceroid2Proxy
# ============================================================


def post_talk(text):
    """
    同步 HTTP POST。

    每一条文本前都加入 <interrupt_disable>。

    SimpleVoiceroid2Proxy 会：
      1. 先解析 <interrupt_disable>
      2. interrupt = false
      3. 从真正文本中删除该命令
      4. 将实际文本写入内部朗读队列

    因此即使 SimpleVoiceroid2Proxy 曾经重启，
    每次提交 EEW 时也都会重新确保 interrupt 已关闭。
    """

    response = http_session.post(
        VR_URL, json={"text": "<interrupt_disable>" + text}, timeout=3
    )

    response.raise_for_status()


async def voice_worker():
    """
    Python 侧的可靠发送队列。

    它不需要等待 VOICEROID2 实际朗读完成。

    SimpleVoiceroid2Proxy 自己拥有内部 FIFO 队列，
    interrupt=false 时会等待上一条语音真正结束后
    再开始下一条。

    Python 这一层主要用于：
      - 保持收到 EEW 的先后顺序
      - SimpleVoiceroid2Proxy 临时故障时不直接丢报
    """

    while True:
        event_id, serial, text = await voice_queue.get()

        try:
            while True:
                try:
                    await asyncio.to_thread(post_talk, text)

                    log(f"Voice queued: " f"EventID={event_id}, Serial={serial}")

                    print("EEW情報更新:")
                    print(text)
                    print()

                    break

                except Exception as e:
                    log(f"VOICEROID Error: {e} " f"(retry in {VOICE_RETRY_DELAY}s)")

                    await asyncio.sleep(VOICE_RETRY_DELAY)

        finally:
            voice_queue.task_done()


# ============================================================
# JMA EEW processing
# ============================================================


async def process_eew(data):
    global initialized

    event_id = data.get("EventID")
    serial = data.get("Serial")

    if event_id is None:
        log("Invalid JMA EEW: EventID missing")
        return

    if serial is None:
        log(f"Invalid JMA EEW: Serial missing " f"(EventID={event_id})")
        return

    # --------------------------------------------------------
    # 1. 同一个 EventID + Serial 不处理第二次
    # --------------------------------------------------------

    if not mark_report_seen(event_id, serial):
        log(f"Duplicate report ignored: " f"EventID={event_id}, Serial={serial}")
        return

    # --------------------------------------------------------
    # 2. 构造实际朗读文本
    # --------------------------------------------------------

    text = build_voice_text(data)

    # --------------------------------------------------------
    # 3. 启动时的当前最新 EEW 不朗读
    #
    # 与原脚本：
    #
    # first = True
    #
    # 的行为保持一致。
    #
    # WebSocket 建立后发送 query_jmaeew，
    # 得到当前已经存在的最新报，仅作为初始状态。
    # --------------------------------------------------------

    if not initialized:
        initialized = True

        set_last_event_text(event_id, text)

        log(f"Initial EEW skipped: " f"EventID={event_id}, Serial={serial}")

        return

    # --------------------------------------------------------
    # 4. 同一地震事件中，朗读内容完全相同则跳过
    # --------------------------------------------------------

    previous_text = get_last_event_text(event_id)

    if previous_text == text:
        log(f"EEW skipped (same voice text): " f"EventID={event_id}, Serial={serial}")

        return

    # 从这一刻开始认为该文本已经进入朗读流程
    set_last_event_text(event_id, text)

    # --------------------------------------------------------
    # 5. 加入语音队列
    # --------------------------------------------------------

    if data.get("isCancel"):
        log(f"JMA EEW cancellation received: " f"EventID={event_id}, Serial={serial}")

    else:
        log(
            f"JMA EEW received: "
            f"EventID={event_id}, "
            f"Serial={serial}, "
            f"Hypocenter={data.get('Hypocenter')}, "
            f"M={data.get('Magnitude')}, "
            f"MaxIntensity={data.get('MaxIntensity')}"
        )

    await voice_queue.put((event_id, serial, text))


# ============================================================
# WebSocket
# ============================================================


async def websocket_client():
    """
    Wolfx JMA EEW WebSocket client。
    """

    while True:
        try:
            async with connect(
                WS_URL,
                user_agent_header=USER_AGENT,
                # WebSocket 协议层 Ping/Pong。
                #
                # 与 Wolfx JSON heartbeat → "ping"
                # 是不同的两层机制。
                ping_interval=30,
                ping_timeout=10,
                open_timeout=10,
                close_timeout=10,
                # EEW JSON 很小，1 MiB 足够
                max_size=1024 * 1024,
            ) as websocket:

                log("WebSocket connected")

                # 获取当前最新 JMA EEW。
                #
                # 首次启动：
                #   仅用于初始化，不朗读。
                #
                # 断线重连：
                #   用于尽可能补回断线期间最新的一报。
                await websocket.send("query_jmaeew")

                async for message in websocket:

                    # ------------------------------------------------
                    # WebSocket Text message
                    # ------------------------------------------------

                    if isinstance(message, bytes):
                        try:
                            message = message.decode("utf-8")

                        except UnicodeDecodeError:
                            log("Invalid binary WebSocket message")
                            continue

                    # ------------------------------------------------
                    # JSON parsing
                    # ------------------------------------------------

                    try:
                        data = json.loads(message)

                    except json.JSONDecodeError:
                        log(f"Invalid JSON received: " f"{message!r}")
                        continue

                    if not isinstance(data, dict):
                        log(f"Unexpected WebSocket JSON: " f"{data!r}")
                        continue

                    message_type = data.get("type")

                    # ------------------------------------------------
                    # Wolfx application-level heartbeat
                    # ------------------------------------------------

                    if message_type == "heartbeat":
                        try:
                            await websocket.send("ping")

                        except ConnectionClosed:
                            raise

                        continue

                    # ------------------------------------------------
                    # Wolfx Pong
                    # ------------------------------------------------

                    if message_type == "pong":
                        continue

                    # ------------------------------------------------
                    # JMA EEW
                    # ------------------------------------------------

                    if message_type == "jma_eew":
                        await process_eew(data)
                        continue

                    # ------------------------------------------------
                    # Unknown message
                    # ------------------------------------------------

                    log(f"Unknown WebSocket message type: " f"{message_type!r}")

        except asyncio.CancelledError:
            raise

        except ConnectionClosed as e:
            log(f"WebSocket disconnected: {e}")

        except Exception as e:
            log(f"WebSocket Error: " f"{type(e).__name__}: {e}")

        log(f"Reconnect in {RECONNECT_DELAY}s...")

        await asyncio.sleep(RECONNECT_DELAY)


# ============================================================
# Main
# ============================================================


async def main():
    log("jma2vr starting")

    # Python -> SimpleVoiceroid2Proxy 队列处理器
    voice_task = asyncio.create_task(voice_worker())

    try:
        await websocket_client()

    finally:
        voice_task.cancel()

        try:
            await voice_task

        except asyncio.CancelledError:
            pass

        http_session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print()
        log("Stopped")
