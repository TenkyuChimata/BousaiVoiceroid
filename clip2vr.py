# -*- coding: utf-8 -*-
import time
import requests
import pyperclip
import threading

url = "http://127.0.0.1:4532/talk?text="
clip_list = []
clip_str_2 = pyperclip.paste()

def voice():
    while(1):
        if clip_list != []:
            requests.get(url + clip_list[0], timeout = 1)
            print("EEW情報更新:")
            print(clip_list[0] + "\n")
            del clip_list[0]
            time.sleep(8)
        time.sleep(0.1)

def main():
    global clip_list, clip_str_2
    while(1):
        clip_str = pyperclip.paste()
        if(clip_str != clip_str_2):
            clip_list.append(clip_str)
        clip_str_2 = pyperclip.paste()
        time.sleep(0.1)

thread1 = threading.Thread(target = voice)
thread2 = threading.Thread(target = main)
thread1.start()
time.sleep(1)
thread2.start()
