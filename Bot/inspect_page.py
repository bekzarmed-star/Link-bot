# -*- coding: utf-8 -*-
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

USER, PASS = "HZT18011983", "Hzt18@11983"
ORG = "Zarmed Pratiksha Hospital Grup"

opts = Options(); opts.add_argument("--window-size=1600,1000")
d = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
w = WebDriverWait(d, 30)

def login():
    d.get("https://mis2.ssv.uz/service-requests")
    time.sleep(2)
    if "login" in d.current_url:
        w.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button.login__card_btn"))).click()
        w.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='username']"))).send_keys(USER)
        d.find_element(By.CSS_SELECTOR, "input[name='password']").send_keys(PASS)
        d.find_element(By.CSS_SELECTOR, "button[type='submit']").click(); time.sleep(4)
    if "select-organization" in d.current_url:
        w.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input.el-input__inner"))).click(); time.sleep(1)
        [x.click() for x in d.find_elements(By.CSS_SELECTOR, ".el-select-dropdown__item span") if ORG in x.text]
        d.find_element(By.CSS_SELECTOR, "button.login__card_btn").click(); time.sleep(4)

login()
for birth in ["25.12.1946", "25-12-1946"]:
    d.get("https://mis2.ssv.uz/service-requests"); time.sleep(2)
    for inp in d.find_elements(By.CSS_SELECTOR, "input.el-input__inner"):
        if "рожд" in (inp.get_attribute("placeholder") or "").lower():
            inp.clear(); inp.send_keys(birth); break
    [b.click() for b in d.find_elements(By.CSS_SELECTOR, "button") if "ильтр" in (b.text or "")]
    time.sleep(4)
    eyes = d.find_elements(By.CSS_SELECTOR, "a.button-show__item, a[href*='/orders/']")
    print("BIRTH", birth, "eyes", len(eyes), [e.get_attribute("href") for e in eyes[:3]])
    # table html snippet
    rows = d.find_elements(By.CSS_SELECTOR, ".el-table__row, tbody tr")
    print("rows", len(rows))
    if rows:
        print("row0 text:", rows[0].text[:200])
d.quit()
