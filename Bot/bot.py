# -*- coding: utf-8 -*-
"""
MIS2 SSV bot — after login:
  1. Направления
  2. Дата рождения
  3. Фильтр
  4. Eye icon (open order)
  5. Запросить бюджет + confirm dialog
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
LOG_PATH = Path(__file__).resolve().parent / "bot.log"
BASE_URL = "https://mis2.ssv.uz"
DIRECTIONS_URL = f"{BASE_URL}/service-requests"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("mis2-bot")

EYE_SELECTORS = (
    "a.button-show__item",
    "a[href*='/orders/']",
    ".fa-eye",
    "i.fa-eye",
)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error("Missing config.json")
        sys.exit(1)
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def create_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1600,1000")
    options.add_argument("--lang=ru-RU")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def format_ts(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S") + f".{dt.microsecond // 1000:03d}"


def parse_clock_time(value: str) -> tuple[int, int, int]:
    parts = value.strip().split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    s = int(parts[2]) if len(parts) > 2 else 0
    return h, m, s


def next_budget_slot(
    now: datetime,
    start_h: int,
    start_m: int,
    start_s: int,
    interval_minutes: int,
    end_h: int | None = None,
    end_m: int | None = None,
    end_s: int | None = None,
) -> datetime:
    """Next slot at start_time + N * interval, within the daily end_time window."""
    interval_sec = interval_minutes * 60
    start = now.replace(hour=start_h, minute=start_m, second=start_s, microsecond=0)
    end = (
        now.replace(hour=end_h, minute=end_m, second=end_s, microsecond=0)
        if end_h is not None
        else None
    )

    if end is not None and now > end:
        tomorrow = now + timedelta(days=1)
        return tomorrow.replace(hour=start_h, minute=start_m, second=start_s, microsecond=0)

    if now <= start:
        return start

    elapsed = (now - start).total_seconds()
    step = int(elapsed // interval_sec)
    candidate = start + timedelta(seconds=step * interval_sec)

    if now <= candidate:
        slot = candidate
    else:
        slot = candidate + timedelta(seconds=interval_sec)

    if end is not None and slot > end:
        tomorrow = now + timedelta(days=1)
        return tomorrow.replace(hour=start_h, minute=start_m, second=start_s, microsecond=0)

    return slot


def wait_until_precise(target: datetime) -> None:
    """Sleep until target wall-clock time (second/millisecond precision)."""
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            return
        if remaining > 2:
            time.sleep(remaining - 1)
        elif remaining > 0.02:
            time.sleep(0.005)
        # else busy-wait for the last ~20 ms


def load_schedule_config(cfg: dict) -> dict:
    sched = cfg.get("schedule", {})
    start_time = sched.get("start_time", cfg.get("start_time", "09:00:00"))
    h, m, s = parse_clock_time(start_time)
    end_h, end_m, end_s = None, None, None
    end_time = sched.get("end_time")
    if end_time:
        end_h, end_m, end_s = parse_clock_time(end_time)
    return {
        "start_h": h,
        "start_m": m,
        "start_s": s,
        "end_h": end_h,
        "end_m": end_m,
        "end_s": end_s,
        "interval_minutes": int(sched.get("interval_minutes", cfg.get("interval_minutes", 30))),
        "prep_seconds": int(sched.get("prep_seconds", 55)),
        "patient_stagger_seconds": int(sched.get("patient_stagger_seconds", 3)),
    }


def make_waits(driver: webdriver.Chrome, cfg: dict) -> tuple:
    main = int(cfg.get("wait_seconds", 30))
    return (
        WebDriverWait(driver, main),
        WebDriverWait(driver, int(cfg.get("pinfl_wait_seconds", 5))),
        WebDriverWait(driver, int(cfg.get("filter_wait_seconds", 30))),
    )


def is_login_page(driver: webdriver.Chrome) -> bool:
    return "/login" in driver.current_url


def is_org_page(driver: webdriver.Chrome) -> bool:
    return "select-organization" in driver.current_url


def is_oauth_authorize_page(driver: webdriver.Chrome) -> bool:
    url = driver.current_url.lower()
    if "oauth" in url and "authorize" in url:
        return True
    try:
        src = driver.page_source.lower()
    except WebDriverException:
        return False
    markers = (
        "authorization request",
        "requesting permission to access your account",
        "authorized to access",
        "mis2.ssv.uz is requesting",
    )
    return any(marker in src for marker in markers)


def _radio_container_text(radio) -> str:
    for xpath in (
        "./ancestor::label[1]",
        "./parent::*",
        "./ancestor::div[1]",
        "./ancestor::li[1]",
        "./ancestor::tr[1]",
    ):
        try:
            container = radio.find_element(By.XPATH, xpath)
            text = (container.text or "").strip()
            if text:
                return text
        except Exception:
            continue
    return ""


def handle_oauth_authorization(driver: webdriver.Chrome, wait: WebDriverWait, org_name: str) -> None:
    """SSO consent screen: pick organization and click Authorize."""
    if not is_oauth_authorize_page(driver):
        return

    log.info("OAuth authorization page detected")
    org_key = org_name.strip().lower()
    selected = False

    radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")
    for radio in radios:
        text = _radio_container_text(radio).lower()
        if org_key and org_key in text:
            js_click(driver, radio)
            selected = True
            log.info("OAuth: selected organization: %s", org_name)
            break

    if not selected and len(radios) >= 2:
        js_click(driver, radios[1])
        selected = True
        log.info("OAuth: selected second organization option (fallback)")

    if not selected and radios:
        js_click(driver, radios[0])
        selected = True
        log.warning("OAuth: selected first organization option (fallback)")

    if not selected:
        raise RuntimeError(f"OAuth authorize: organization not found: {org_name}")

    time.sleep(0.5)

    authorize_btn = None
    for btn in driver.find_elements(By.CSS_SELECTOR, "button, input[type='submit'], a"):
        text = (btn.text or btn.get_attribute("value") or "").strip().lower()
        if not text:
            continue
        if any(word in text for word in ("cancel", "отмен", "deny", "отказ")):
            continue
        if "authoriz" in text or "разреш" in text or text in ("ok", "да", "yes"):
            authorize_btn = btn
            break

    if authorize_btn is None:
        for btn in driver.find_elements(By.CSS_SELECTOR, "button.btn-success, button[type='submit']"):
            text = (btn.text or btn.get_attribute("value") or "").strip().lower()
            if text and "cancel" not in text and "отмен" not in text:
                authorize_btn = btn
                break

    if authorize_btn is None:
        raise RuntimeError("OAuth authorize: Authorize button not found")

    js_click(driver, authorize_btn)
    log.info("OAuth: clicked Authorize")
    time.sleep(3)


def handle_oauth_authorization_if_present(
    driver: webdriver.Chrome, wait: WebDriverWait, org_name: str
) -> None:
    try:
        WebDriverWait(driver, 8).until(lambda d: is_oauth_authorize_page(d))
    except TimeoutException:
        return
    handle_oauth_authorization(driver, wait, org_name)


def js_click(driver: webdriver.Chrome, element) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
    time.sleep(0.3)
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def login_sso(driver: webdriver.Chrome, wait: WebDriverWait, cfg: dict) -> None:
    login_cfg = cfg["login"]
    if not is_login_page(driver):
        driver.get(f"{BASE_URL}/login")
    wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button.login__card_btn"))).click()
    user = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='username']")))
    user.clear()
    user.send_keys(login_cfg["username"])
    pw = driver.find_element(By.CSS_SELECTOR, "input[name='password']")
    pw.clear()
    pw.send_keys(login_cfg["password"])
    driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
    time.sleep(2)
    handle_oauth_authorization_if_present(driver, wait, cfg.get("organization", ""))
    time.sleep(2)
    log.info("SSO login submitted")


def select_organization(driver: webdriver.Chrome, wait: WebDriverWait, org_name: str) -> None:
    wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input.el-input__inner"))).click()
    time.sleep(1)
    for item in driver.find_elements(By.CSS_SELECTOR, ".el-select-dropdown__item"):
        if org_name in (item.text or ""):
            item.click()
            log.info("Organization selected: %s", org_name)
            break
    else:
        raise RuntimeError(f"Organization not found: {org_name}")
    time.sleep(1)
    driver.find_element(By.CSS_SELECTOR, "button.login__card_btn").click()
    time.sleep(3)
    log.info("Organization saved")


def ensure_session(driver: webdriver.Chrome, wait: WebDriverWait, cfg: dict) -> None:
    driver.get(DIRECTIONS_URL)
    time.sleep(2)
    if is_login_page(driver):
        login_sso(driver, wait, cfg)
    else:
        handle_oauth_authorization_if_present(driver, wait, cfg.get("organization", ""))
    if is_org_page(driver):
        select_organization(driver, wait, cfg["organization"])
    time.sleep(2)


def normalize_birth_date(value: str) -> str:
    v = value.strip()
    if re.match(r"^\d{2}-\d{2}-\d{4}$", v):
        return v.replace("-", ".")
    return v


def go_to_directions(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    """Step 1: Направления."""
    driver.get(DIRECTIONS_URL)
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input.el-input__inner")))
    except TimeoutException:
        pass
    log.info("Step 1: opened Направления")
    time.sleep(2)


def find_birth_date_input(driver: webdriver.Chrome, wait: WebDriverWait):
    return wait.until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//input[contains(@placeholder, 'рожд') or contains(@placeholder, 'Рожд')"
                " or contains(@placeholder, 'tug')]",
            )
        )
    )


def clear_filters_if_present(driver: webdriver.Chrome) -> None:
    for btn in driver.find_elements(By.CSS_SELECTOR, "button, .el-button"):
        text = (btn.text or "").strip().lower()
        if "очист" in text and "фильтр" in text:
            js_click(driver, btn)
            log.info("Cleared filters")
            time.sleep(2)
            return


def fill_birth_date(driver: webdriver.Chrome, wait: WebDriverWait, birth_date: str) -> None:
    """Step 2: Дата рождения."""
    inp = find_birth_date_input(driver, wait)
    inp.click()
    inp.clear()
    time.sleep(0.2)
    inp.send_keys(normalize_birth_date(birth_date))
    log.info("Step 2: birth date entered: %s", birth_date)


def click_filter(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    """Step 3: Фильтр."""
    btn = wait.until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                "//button[contains(normalize-space(.), 'Фильтр') or "
                "contains(normalize-space(.), 'фильтр')]",
            )
        )
    )
    js_click(driver, btn)
    log.info("Step 3: clicked Фильтр")


def wait_for_filter_results(driver: webdriver.Chrome, filter_wait: WebDriverWait) -> None:
    """Wait until order links appear in the table after filtering."""
    def _has_results(d: webdriver.Chrome) -> bool:
        for sel in ("a.button-show__item", "a[href*='/orders/']"):
            for el in d.find_elements(By.CSS_SELECTOR, sel):
                href = el.get_attribute("href") or ""
                if "/orders/" in href and el.is_displayed():
                    return True
        return False

    filter_wait.until(_has_results)
    time.sleep(1)
    log.info("Filter results loaded")


def collect_eye_links(driver: webdriver.Chrome) -> list:
    seen = set()
    links = []
    for sel in EYE_SELECTORS:
        for el in driver.find_elements(By.CSS_SELECTOR, sel):
            if sel.endswith("fa-eye"):
                try:
                    el = el.find_element(By.XPATH, "./ancestor::a[1]")
                except Exception:
                    continue
            href = el.get_attribute("href") or ""
            if "/orders/" not in href:
                continue
            if href in seen:
                continue
            if el.is_displayed():
                seen.add(href)
                links.append(el)
    return links


def row_contains_pinfl(row, pinfl: str) -> bool:
    text = (row.text or "").replace(" ", "").replace("\u00a0", "")
    return pinfl in text


def find_eye_for_pinfl(driver: webdriver.Chrome, pinfl: str, short_wait: WebDriverWait):
    """Find eye link in table row matching PINFL (quick timeout)."""
    try:
        short_wait.until(
            lambda d: any(
                row_contains_pinfl(r, pinfl)
                for r in d.find_elements(By.CSS_SELECTOR, ".el-table__row, tbody tr")
            )
        )
    except TimeoutException:
        return None

    for row in driver.find_elements(By.CSS_SELECTOR, ".el-table__row, tbody tr"):
        if not row_contains_pinfl(row, pinfl):
            continue
        for sel in ("a.button-show__item", "a[href*='/orders/']"):
            try:
                link = row.find_element(By.CSS_SELECTOR, sel)
                if link.is_displayed():
                    return link
            except Exception:
                continue
    return None


def open_order_via_eye(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    short_wait: WebDriverWait,
    filter_wait: WebDriverWait,
    pinfl: str | None,
    order_url: str | None,
) -> str:
    """Step 4: click eye and wait for order page. Returns order URL."""
    wait_for_filter_results(driver, filter_wait)

    eye = None
    if pinfl:
        eye = find_eye_for_pinfl(driver, pinfl, short_wait)
        if eye:
            log.info("Step 4: found eye for PINFL %s", pinfl)

    if eye is None:
        links = collect_eye_links(driver)
        if links:
            eye = links[0]
            log.info("Step 4: using first eye link -> %s", eye.get_attribute("href"))

    if eye is not None:
        href = eye.get_attribute("href")
        js_click(driver, eye)
        log.info("Step 4: clicked eye -> %s", href)
        wait.until(lambda d: "/orders/" in d.current_url)
        time.sleep(2)
        return driver.current_url

    if order_url and "/orders/" in order_url:
        log.warning("Step 4: eye not found, opening order URL directly: %s", order_url)
        driver.get(order_url)
        wait.until(lambda d: "/orders/" in d.current_url)
        time.sleep(2)
        return driver.current_url

    raise RuntimeError("Step 4 failed: no eye link and no order_url fallback")


def click_request_budget(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    reason: str,
    click_at: datetime | None = None,
) -> None:
    """Step 5: Запросить бюджет at exact time, then confirm dialog."""
    if "/orders/" not in driver.current_url:
        raise RuntimeError(f"Step 5 failed: not on order page ({driver.current_url})")

    budget_btn = wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "button.el-button--warning"))
    )
    if not budget_btn.is_displayed():
        raise RuntimeError("Step 5 failed: Запросить бюджет button not visible")

    if click_at is not None:
        remaining = (click_at - datetime.now()).total_seconds()
        log.info(
            "Step 5: ready on order page, waiting %.2fs until budget click at %s",
            max(0, remaining),
            format_ts(click_at),
        )
        wait_until_precise(click_at)

    js_click(driver, budget_btn)
    clicked = datetime.now()
    log.info("Step 5: clicked Запросить бюджет at %s", format_ts(clicked))
    time.sleep(1)

    dialog_wait = WebDriverWait(driver, 8)
    try:
        dialog = dialog_wait.until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, ".el-dialog"))
        )
    except TimeoutException:
        log.info("Step 5: no dialog (budget may already be requested)")
        return

    for inp in dialog.find_elements(By.CSS_SELECTOR, "textarea"):
        if inp.is_displayed():
            inp.clear()
            inp.send_keys(reason)
            log.info("Step 5: reason entered: %s", reason)
            break

    confirm = None
    footer = dialog.find_elements(By.CSS_SELECTOR, ".el-dialog__footer button, .el-dialog__footer .el-button")
    for btn in footer:
        text = (btn.text or "").strip().lower()
        cls = btn.get_attribute("class") or ""
        if "primary" in cls or "запрос" in text or "подтвер" in text or "сохран" in text:
            if "отмен" not in text and "нет" != text:
                confirm = btn
                break

    if confirm is None:
        for btn in dialog.find_elements(By.CSS_SELECTOR, "button"):
            text = (btn.text or "").strip().lower()
            if text and "отмен" not in text and text != "нет":
                confirm = btn

    if confirm is None:
        raise RuntimeError("Step 5 failed: confirm button not found in budget dialog")

    js_click(driver, confirm)
    log.info("Step 5: budget dialog confirmed (%s)", (confirm.text or "").strip())
    time.sleep(2)


def process_patient(
    driver: webdriver.Chrome,
    waits: tuple,
    patient: dict,
    cfg: dict,
    budget_click_at: datetime | None = None,
) -> None:
    wait, short_wait, filter_wait = waits
    label = f"{patient.get('last_name', '')} {patient.get('first_name', '')}".strip()
    birth = patient["birth_date"]
    pinfl = patient.get("pinfl")
    order_url = patient.get("order_url")
    reason = patient.get("budget_reason") or cfg.get("budget_reason", "Budjeti yetarli emas")

    log.info("--- Processing %s (DOB %s) ---", label or "patient", birth)
    if budget_click_at:
        log.info("Target budget click: %s", format_ts(budget_click_at))

    prep_started = datetime.now()
    go_to_directions(driver, wait)
    clear_filters_if_present(driver)
    fill_birth_date(driver, wait, birth)
    click_filter(driver, wait)
    open_order_via_eye(driver, wait, short_wait, filter_wait, pinfl, order_url)

    prep_done = datetime.now()
    prep_secs = (prep_done - prep_started).total_seconds()
    log.info("Steps 1-4 finished in %.1fs", prep_secs)

    if budget_click_at and datetime.now() > budget_click_at:
        late = (datetime.now() - budget_click_at).total_seconds()
        log.warning("Steps 1-4 took too long (+%.1fs late) - clicking budget immediately", late)

    click_request_budget(driver, wait, reason, click_at=budget_click_at)
    log.info("Done: %s", label)


def run_job(config: dict, budget_slot: datetime | None = None) -> None:
    sched = load_schedule_config(config)
    started = datetime.now()
    log.info("=== Job started at %s ===", format_ts(started))

    if budget_slot:
        log.info("=== Budget slot: %s ===", format_ts(budget_slot))

    driver = None
    try:
        driver = create_driver(config.get("headless", False))
        waits = make_waits(driver, config)

        ensure_session(driver, waits[0], config)

        patients = config.get("patients", [])
        if not patients:
            log.error("No patients in config.json")
            return

        stagger = sched["patient_stagger_seconds"]

        for i, patient in enumerate(patients, start=1):
            if not patient.get("birth_date"):
                log.warning("Patient %s skipped - missing birth_date", i)
                continue
            click_at = None
            if budget_slot:
                click_at = budget_slot + timedelta(seconds=(i - 1) * stagger)
            try:
                process_patient(driver, waits, patient, config, budget_click_at=click_at)
            except Exception:
                log.exception("Failed for patient %s", i)
                go_to_directions(driver, waits[0])

        finished = datetime.now()
        log.info("Job finished at %s (duration %.1fs)", format_ts(finished), (finished - started).total_seconds())
    except TimeoutException:
        log.exception("Timed out - check internet or page layout.")
    except WebDriverException:
        log.exception("Browser error - is Chrome installed?")
    except Exception:
        log.exception("Job failed.")
    finally:
        if driver is not None:
            driver.quit()


def main() -> None:
    config = load_config()
    sched = load_schedule_config(config)
    anchor = f"{sched['start_h']:02d}:{sched['start_m']:02d}:{sched['start_s']:02d}"
    if sched["end_h"] is not None:
        end_anchor = f"{sched['end_h']:02d}:{sched['end_m']:02d}:{sched['end_s']:02d}"
        window = f"{anchor} to {end_anchor}"
    else:
        window = f"{anchor} onwards"

    log.info(
        "MIS2 bot started - budget clicks %s every %s min (exact schedule)",
        window,
        sched["interval_minutes"],
    )
    log.info("Prep starts %s seconds before each click (steps 1-4)", sched["prep_seconds"])
    log.info("Press Ctrl+C to stop.")

    while True:
        now = datetime.now()
        patients = [p for p in config.get("patients", []) if p.get("birth_date")]
        patient_count = max(len(patients), 1)
        prep_each = sched["prep_seconds"]
        stagger = sched["patient_stagger_seconds"]

        slot = next_budget_slot(
            now,
            sched["start_h"],
            sched["start_m"],
            sched["start_s"],
            sched["interval_minutes"],
            sched["end_h"],
            sched["end_m"],
            sched["end_s"],
        )
        # Enough time for each patient: prep + stagger between budget clicks
        total_prep = prep_each * patient_count + stagger * max(0, patient_count - 1)
        job_start = slot - timedelta(seconds=total_prep)

        if now < job_start:
            log.info(
                "Next budget click: %s | prep starts: %s | waiting %.0fs (%s patients)",
                format_ts(slot),
                format_ts(job_start),
                (job_start - now).total_seconds(),
                patient_count,
            )
            wait_until_precise(job_start)
        elif now < slot:
            log.info(
                "In prep window - budget click at %s (%.0fs left)",
                format_ts(slot),
                (slot - now).total_seconds(),
            )

        run_job(config, budget_slot=slot)

        now = datetime.now()
        next_slot = next_budget_slot(
            now + timedelta(seconds=1),
            sched["start_h"],
            sched["start_m"],
            sched["start_s"],
            sched["interval_minutes"],
            sched["end_h"],
            sched["end_m"],
            sched["end_s"],
        )
        next_total_prep = prep_each * patient_count + stagger * max(0, patient_count - 1)
        next_prep = next_slot - timedelta(seconds=next_total_prep)
        if next_prep > now:
            wait_until_precise(next_prep)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user.")
