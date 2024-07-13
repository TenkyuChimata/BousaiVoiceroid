# -*- coding: utf-8 -*-
import time
import datetime
import requests
import threading

vr_url = "http://127.0.0.1:4532/talk?text="
jma_url = "https://api.wolfx.jp/jma_eew.json"
headers = {"User-Agent": "jma2vr/1.0"}
eew_list = []
first = True
eew_info_2 = None

def voice():
    while True:
        try:
            if eew_list != []:
                requests.get(vr_url + eew_list[0], timeout = 3)
                print("EEW情報更新:")
                print(eew_list[0] + "\n")
                del eew_list[0]
                time.sleep(8)
        except Exception as e:
            print(f"{datetime.datetime.now().strftime('[%H:%M:%S]')} Error: {e}")
            time.sleep(0.1)
            continue
        time.sleep(0.1)

def main():
    global first, eew_list, eew_info_2
    while True:
        try:
            eew = requests.get(jma_url, headers = headers, timeout = 3)
            eew_json = eew.json()
            eew_info = eew_json["Hypocenter"] + "で地震\n" + "マグニチュード" + str(eew_json["Magunitude"]) + "\n推定さたん最大震度は、" + eew_json["MaxIntensity"] + "です"
            if not first:
                if eew_info != eew_info_2:
                    eew_list.append(eew_info)
                    eew_info_2 = eew_info
            else:
                eew_info_2 = eew_info
                first = False
            time.sleep(1)
        except:
            time.sleep(0.5)
            continue

thread1 = threading.Thread(target = voice)
thread2 = threading.Thread(target = main)
thread1.start()
time.sleep(1)
thread2.start()
