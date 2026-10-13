"""Reading the account: order batches with production progress, messages, parts, billing, and more.

Every function takes an open `Session` and returns plain data (dicts, lists, strings), so the command
line, the MCP server and your own scripts share them. Only `message()` changes anything: opening a
message is what marks it read on the site, so it goes through the gate as a logged write.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

from .app import DATA, JlcError, Session

CHINA = datetime.timezone(datetime.timedelta(hours=8))
KIND = {"order_pcb": "PCB", "order_smt": "PCBA", "order_steel": "stencil", "order_tdp": "3D print",
        "order_cnc": "CNC", 0: "PCB", 1: "PCB", 3: "stencil", 4: "PCBA", 7: "3D print", 8: "CNC"}
LIBRARY = {"base": "basic", "expand": "extended"}
BATCH_QUERY = {"businessType": None, "orderStatisticsType": None, "searchKey": None, "businessTypeStr": None,
               "waitPayOrSupplement": None, "waitBizConfirm": None, "inProduction": None, "shipped": None,
               "batchStatus": None, "fromType": 3, "orderBusinessSystemType": "0", "currentPage": 1,
               "pageRows": 10, "prevBatchNum": None}


def when(value: Any) -> str | None:
    """A JLC time as local 'YYYY-MM-DD HH:MM'. Numbers are epoch milliseconds; text is China time
    (an order carries both forms of the same moment, which is how the zone was checked)."""
    if value in (None, "", 0, "0"):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            moment = datetime.datetime.fromtimestamp(int(value) / 1000, datetime.timezone.utc)
        else:
            moment = datetime.datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA)
    except (ValueError, OverflowError, OSError):
        return str(value)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def batch_summary(batch: dict) -> dict:
    """One row of the order list: what it is, where it stands, and anything JLC is waiting on."""
    paid, unpaid = batch.get("payUnionSecondVO") or {}, batch.get("unPayUnionSecondVO") or {}
    payment, express = batch.get("paymentInfoVO") or {}, batch.get("expressInfoVO") or {}
    orders = []
    for source, is_paid in ((paid, True), (unpaid, False)):
        for item in source.get("orderInfoVOList") or []:
            waiting = [t for t in item.get("awaitConfirmationTypes") or [] if t and t != "none"]
            if item.get("waitConfirm") and not waiting:
                waiting = ["confirmation"]
            orders.append({"kind": KIND.get(item.get("orderType"), item.get("orderType")),
                           "code": item.get("orderCode"), "status": item.get("orderStatus"),
                           "paid": is_paid, "waiting_for_you": waiting})
    return {"batch": batch.get("batchNum"), "created": when(batch.get("batchCreateTime")),
            "status": batch.get("batchStatus"), "total": paid.get("totalFee") or unpaid.get("totalFee"),
            "currency": (batch.get("settleCurrencyInfoVO") or {}).get("settleCurrency") or "USD",
            "shipping": express.get("freightModeName"), "tracking": express.get("expressNo") or None,
            "express_type": express.get("expressType"),
            "supplement": bool(payment.get("hadSuppleFlag")), "supplement_reason": payment.get("supplementReason"),
            "unpaid": bool(unpaid.get("orderInfoVOList")), "orders": orders}


def batches(jlc: Session, limit: int = 10, search: str | None = None) -> list[dict]:
    """Recent order batches, newest first, flagging anything JLC waits on."""
    data = jlc.api("POST", "/overseas-core-platform/orderCenter/selectPersonBatch",
                   dict(BATCH_QUERY, pageRows=limit, searchKey=search))
    return [batch_summary(b) for b in (data or {}).get("list") or []]


def _batch(jlc: Session, batch: str) -> dict:
    listed = next((b for b in batches(jlc, limit=5, search=batch) if b["batch"] == batch), None)
    if listed is None:
        raise JlcError(f"{batch}: not among this account's orders")
    return listed


def pcb_progress(jlc: Session, produce_order_num: str) -> dict:
    """The bare board's production steps; JLC answers 'No record' until it publishes any."""
    try:
        data = jlc.api("POST", "/overseas-core-platform/order/orderWipProcessGroupedProgress",
                       {"produceOrderNum": produce_order_num})
    except JlcError as error:
        if "No record" in str(error):
            return {"percent": 0, "done": 0, "of": 0, "eta": None, "steps": []}
        raise
    steps = [{"step": p.get("overseasFrontName") or p.get("technicsProcessName"),
              "done": when(p.get("endTime") or p.get("finishTime") or p.get("completeTime"))}
             for group in data.get("processGroups") or [] for p in group.get("processes") or []]
    return {"percent": data.get("produceWipPercentage"), "done": data.get("completedProcessCount"),
            "of": len(steps), "eta": when(data.get("estimateAchieveDate")), "steps": steps}


def smt_progress(jlc: Session, smt_order_access_id: str) -> dict:
    """Assembly steps, the finish date the page shows, and how many X-ray images JLC has attached. Until
    assembly has steps to show, JLC answers HTTP 501 with an empty body (seen minutes after paying)."""
    try:
        data = jlc.api("GET", "/overseas-pcb-order/v1/smtOrder/smtWipProcess", query={"uuid": smt_order_access_id})
    except JlcError as error:
        if "HTTP 501" in str(error):
            return {"percent": 0, "done": 0, "of": 0, "eta": None, "production_estimate": None, "xray_images": 0,
                    "steps": [], "note": "no assembly steps published yet"}
        raise
    steps = [{"step": p.get("technicsName"), "done": when(p.get("endTime"))} for p in data.get("smtList") or []]
    done = sum(1 for s in steps if s["done"])
    return {"percent": round(100 * done / len(steps)) if steps else 0, "done": done, "of": len(steps),
            "eta": when(data.get("smtAchieveDate")), "production_estimate": when(data.get("estimateAchieveDate")),
            "xray_images": len(data.get("xrayFileAccessIds") or []), "steps": steps}


def timeline(jlc: Session, batch: str) -> list[dict]:
    """The order page's history in JLC's own words, newest first: submitted, paid, supplement asked,
    reviewed, an engineering hold ("is pending. Please check your email"), data finished, shipped."""
    data = jlc.api("GET", "/overseas-pcb-order/v1/orderCenter/selectOrderProcessRecordByBatchNum",
                   query={"batchNum": batch})
    return [{"time": when(p.get("processTime")), "type": p.get("processType"), "text": p.get("processRemark")}
            for p in (data or {}).get("batchNumProcessVos") or []]


def order(jlc: Session, batch: str) -> dict:
    """One batch as the order page shows it: every order's production steps and ETA, and the timeline."""
    listed = _batch(jlc, batch)
    detail = jlc.api("GET", "/overseas-core-platform/orderCenter/selectPersonOrderDetail", query={"batchNum": batch})
    common = detail.get("batchOrderDetailCommonVO") or {}
    view = dict(listed, paid_at=when(common.get("payTime")), production_started=when(common.get("produceTime")),
                weight_kg=common.get("totalWeight"))
    view["tracking"] = view["tracking"] or common.get("expressNo") or None
    waiting = {o["code"]: o["waiting_for_you"] for o in listed["orders"]}
    view["orders"] = []
    for row in detail.get("unionOrderDetailVOList") or []:
        record = row.get("recordsDetail") or {}
        inner = record.get("detail") or {}
        entry = {"kind": KIND.get(row.get("orderType"), row.get("orderType")), "code": row.get("orderCode"),
                 "status": record.get("orderStatus"), "build_time": record.get("achieveDateShow") or None}
        if inner.get("smtDetail"):
            smt = inner["smtDetail"]
            entry["code"] = smt.get("smtOrderCode") or entry["code"]
            entry["progress"] = smt_progress(jlc, smt.get("smtOrderAccessId") or record.get("orderNum"))
        elif record.get("produceOrderNum"):
            entry["factory_estimate"] = when((inner.get("pcbDetail") or {}).get("estimateAchieveDate"))
            entry["progress"] = pcb_progress(jlc, record["produceOrderNum"])
        entry["waiting_for_you"] = waiting.get(entry["code"], [])
        view["orders"].append(entry)
    view["timeline"] = timeline(jlc, batch)
    return view


def messages(jlc: Session, limit: int = 20, unread_only: bool = False) -> dict:
    """The message list and unread counts. Never opens a message: opening one is what marks it read."""
    data = jlc.api("POST", "/overseas-pcb-order/v1/msgCustomerMessage/selectCustomerNoticeList",
                   {"currentPage": 1, "pageSize": limit})
    counts = jlc.api("POST", "/overseas-core-platform/shoppingCart/selectMultiTypeUnreadCount", {},
                     query={"languageCode": "en"})
    items = []
    for item in (data or {}).get("list") or []:
        if unread_only and item.get("readStatus"):
            continue
        items.append({"id": item.get("msgNoticeRecordAccessId"), "time": when(item.get("msgTime")),
                      "order": item.get("orderCode") or None,
                      "kind": "order" if item.get("orderCode") or item.get("msgNoticeType") == 3 else "notice",
                      "read": bool(item.get("readStatus")),
                      "title": (item.get("noticeTitle") or "").replace("﻿", "").strip()})
    return {"unread": counts, "messages": items}


def message(jlc: Session, ident: str) -> dict:
    """One message in full. Opening it is what marks it read on the site, so this is a (logged) write."""
    data = jlc.api("POST", "/overseas-pcb-order/v1/msgCustomerMessage/selectMessageNoticeDetail",
                   {"msgNoticeRecordAccessId": ident}, write=True, form=True) or {}
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(data.get("messageContent") or ""))).strip()
    return {"title": data.get("noticeTitle"), "time": when(data.get("msgTime")), "order": data.get("orderCode"),
            "text": text, "raw": data}


def parts(jlc: Session, keyword: str, limit: int = 10) -> dict:
    """The site's own parts search: LCSC code, basic or extended, stock, unit price."""
    data = jlc.api("POST", "/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList",
                   {"keyword": keyword, "currentPage": 1, "pageSize": limit, "searchSource": "search"})
    page = (data or {}).get("componentPageInfo") or {}
    found = []
    for item in page.get("list") or []:
        prices = item.get("componentPrices") or []
        found.append({"code": item.get("componentCode"), "model": item.get("componentModelEn"),
                      "package": item.get("componentSpecificationEn"), "maker": item.get("componentBrandEn"),
                      "library": LIBRARY.get(item.get("componentLibraryType"), item.get("componentLibraryType")),
                      "stock": item.get("stockCount"), "unit_price": prices[0].get("productPrice") if prices else None,
                      "description": item.get("describe")})
    return {"total": page.get("total"), "parts": found}


def _smt_orders(jlc: Session, batch: str) -> list[dict]:
    """The batch's assembly orders: code and access id."""
    detail = jlc.api("GET", "/overseas-core-platform/orderCenter/selectPersonOrderDetail", query={"batchNum": batch})
    found = []
    for row in (detail or {}).get("unionOrderDetailVOList") or []:
        record = row.get("recordsDetail") or {}
        smt = (record.get("detail") or {}).get("smtDetail")
        if smt:
            found.append({"code": smt.get("smtOrderCode"), "access_id": smt.get("smtOrderAccessId"),
                          "finished_pictures": bool(record.get("finishedPictureView"))})
    return found


def track(jlc: Session, batch: str) -> dict:
    """The carrier's tracking events, once JLC has shipped the batch."""
    listed = _batch(jlc, batch)
    if not listed["tracking"]:
        return {"batch": batch, "shipped": False, "shipping": listed["shipping"], "events": None}
    data = jlc.api("POST", "/overseas-core-platform/batchExpressQuery/getTrackingContent",
                   {"expressNo": listed["tracking"], "expressType": listed["express_type"], "batchNum": batch})
    return {"batch": batch, "shipped": True, "shipping": listed["shipping"], "tracking": listed["tracking"],
            "events": data}


def _save(jlc: Session, path: str, query: dict, folder: Path, stem: str) -> str:
    data, kind = jlc.download("GET", path, query=query)
    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "application/pdf": ".pdf",
              "image/webp": ".webp", "application/zip": ".zip"}.get(kind.split(";")[0].strip(), ".bin")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{stem}{suffix}"
    target.write_bytes(data)
    return str(target)


def photos(jlc: Session, batch: str, folder: Path | None = None) -> dict:
    """JLC's pictures of an assembly batch, saved to disk: the finished-board photos it asks you to look
    at, and the images of its X-ray inspection."""
    folder = folder or DATA / "photos" / batch
    saved, notes = [], []
    for smt in _smt_orders(jlc, batch):
        pictures = jlc.api("GET", "/overseas-pcb-order/v1/finishedPicture/viewSmtOrderFinishedPicture",
                           query={"smtOrderCode": smt["code"]}) or {}
        for index, record in enumerate(pictures.get("confirmRecordVos") or [], 1):
            for side in ("top", "bottom"):
                value = record.get(f"{side}PictureUrl")
                if value:
                    saved.append(_save(jlc, "/overseas-pcb-order/v1/finishedPicture/downloadSmtFinishedPicture",
                                       {"fileAccessId": value}, folder, f"{smt['code']}-finished-{index}-{side}"))
        if not pictures.get("confirmRecordVos"):
            notes.append(f"{smt['code']}: no finished-board photos yet")
        xray = jlc.api("GET", "/overseas-pcb-order/v1/smtOrder/smtWipProcess",
                       query={"uuid": smt["access_id"]}).get("xrayFileAccessIds") or []
        for index, file_id in enumerate(xray, 1):
            saved.append(_save(jlc, "/overseas3dpOrderPlatform/tdpFile/downloadTdpFileOld", {"fileAccessId": file_id},
                               folder, f"{smt['code']}-xray-{index}"))
        if not xray:
            notes.append(f"{smt['code']}: no X-ray images yet")
    return {"batch": batch, "saved": saved, "notes": notes}


def confirmations(jlc: Session, batch: str) -> dict:
    """What JLC has put up for confirmation on each order (production files, placement), as the order
    page's "confirm" dialogs show it. Reading it confirms nothing."""
    detail = jlc.api("GET", "/overseas-core-platform/orderCenter/selectPersonOrderDetail", query={"batchNum": batch})
    waiting = {o["code"]: o["waiting_for_you"] for o in _batch(jlc, batch)["orders"]}
    found = []
    for row in (detail or {}).get("unionOrderDetailVOList") or []:
        record = row.get("recordsDetail") or {}
        entry = {"order": row.get("orderCode"), "kind": KIND.get(row.get("orderType"), row.get("orderType")),
                 "status": record.get("confirmFileStatus"),
                 "waiting_for_you": waiting.get(row.get("orderCode"), []), "detail": None}
        # JLC answers an empty HTTP 400 for an order without a confirmation step, so ask only when
        # the order carries one
        if record.get("produceOrderNum") and (record.get("confirmFileStatus") or entry["waiting_for_you"]):
            try:
                entry["detail"] = jlc.api("GET", "/overseas-pcb-order/v1/confirmFile/confirmFileDetail",
                                          query={"orderNum": record["produceOrderNum"], "orderType": row.get("orderType")})
            except JlcError as error:
                entry["detail"] = {"error": str(error)}
        found.append(entry)
    return {"batch": batch, "orders": found}


def bom(jlc: Session, batch: str) -> dict:
    """The parts JLC bought and placed for each assembly order, at the price it charged."""
    orders = []
    for smt in _smt_orders(jlc, batch):
        detail = jlc.api("GET", "/overseas-pcb-order/v1/smtOrder/getSmtOrderDetail",
                         query={"smtOrderNum": smt["access_id"]}) or {}
        usage = jlc.api("POST", "/overseas-pcb-order/v1/smtOrder/querySmtComponent",
                        {"smtOrderNum": smt["access_id"]}) or {}
        orders.append({"code": smt["code"], "boards": detail.get("patchNum"), "side": detail.get("patchLocation"),
                       "parts_total": detail.get("totalPrice"),
                       "from_your_stock": len(usage.get("previouslyList") or []),
                       "consigned": len(usage.get("consignedList") or []),
                       "parts": [{"designators": r.get("designator"), "code": r.get("componentCode"),
                                  "model": r.get("componentModelEn"),
                                  "library": LIBRARY.get(r.get("componentLibraryType"), r.get("componentLibraryType")),
                                  "bought": r.get("componentRealCount"), "unit_price": r.get("unitPrice"),
                                  "total": r.get("extPrice"), "assembly": r.get("assemblyMode")}
                                 for r in detail.get("smtBomResult") or []]})
    return {"batch": batch, "orders": orders}


def inventory(jlc: Session, search: str = "", limit: int = 50) -> list[dict]:
    """Parts JLC holds for this account (the Parts Manager stock), ready for assembly orders."""
    data = jlc.api("GET", "/overseas-smt-component-order-platform/v1/overseasSmtComponentOrder/myLibrary/"
                          "getCustomerComponentStock", query={"pageNum": 1, "pageSize": limit, "keyWord": search})
    return [{"code": r.get("componentCode"), "model": r.get("componentModel") or r.get("componentModelEn"),
             # privateStockCount is the field JLC returns; 0 must stay 0
             "in_stock": next((r[k] for k in ("privateStockCount", "stockNumber", "stockCount", "canUseNumber")
                               if r.get(k) is not None), None), "row": r}
            for r in (data or {}).get("list") or []]


def parts_orders(jlc: Session, search: str = "", limit: int = 10) -> list[dict]:
    """Parts orders (buying parts into the account's library): what, how many, paid, where they are."""
    data = jlc.api("POST", "/overseas-smt-component-order-platform/v1/overseasSmtComponentOrder/presaleOrder/"
                           "selectPresaleOrderList",
                   {"pageNum": 1, "pageSize": limit, "orderType": None, "keyword": search, "orderStatus": None})
    found = []
    for batch in (data or {}).get("list") or []:
        lines = []
        for key, where in (("stockList", "in stock at JLC"), ("buyList", "being bought"),
                           ("overseasShopList", "overseas shop"), ("idleOrderList", "idle stock")):
            for line in batch.get(key) or []:
                goods = line.get("presaleGoodsRecords") or [{}]
                lines.append({"where": where, "order": line.get("presaleOrderNo"), "status": line.get("orderStatus"),
                              "paid": line.get("payStatus"), "money": line.get("settlePaidMoney") or line.get("paidMoney"),
                              "parts": [f"{g.get('componentCode')} {g.get('componentModel') or ''} x{g.get('presaleNumber')}"
                                        f" (bought {g.get('purchasedNumber') or 0}"
                                        + (f" from {g['provider']}" if g.get("provider") else "")
                                        + (f", due {g['deliveryDays']}" if g.get("deliveryDays") else "") + ")"
                                        for g in goods if g.get("componentCode")],
                              # the same parts as data, for `watch` to compare between runs
                              "goods": [{"code": g["componentCode"], "model": g.get("componentModel"),
                                         "wanted": g.get("presaleNumber"), "bought": g.get("purchasedNumber") or 0,
                                         "due": g.get("deliveryDays")} for g in goods if g.get("componentCode")]})
        found.append({"batch": batch.get("orderBatchNo"), "created": when(batch.get("createTime")),
                      "total": batch.get("advanceChargeMoney"), "ready_for_assembly": when(batch.get("readyForAssemblyDate")),
                      "lines": lines})
    return found


def billing(jlc: Session, search: str | None = None, limit: int = 20) -> list[dict]:
    """Every payment: batch, amount, status, and the separate payments inside it (supplements)."""
    data = jlc.api("POST", "/overseas-pcb-order/v1/billing/queryBillingBatchNumList",
                   {"pageNum": 1, "pageSize": limit, "queryTimeType": None, "batchNum": search or None,
                    "startTime": None, "endTime": None})
    return [{"batch": r.get("batchNum"), "amount": r.get("amount"), "currency": r.get("settleCurrency") or "USD",
             "status": r.get("status"), "date": when(r.get("updateDate")),
             "payments": [p.get("amount") for p in r.get("billingPaymentInfoList") or []]}
            for r in (data or {}).get("list") or []]


def invoice(jlc: Session, batch: str) -> Any:
    """The commercial invoice data JLC holds for a paid batch."""
    return jlc.api("POST", "/overseas-core-platform/orderCenter/invoiceOrder", {"batchNum": batch, "orderPay": "yes"})


def coupons(jlc: Session) -> dict:
    """The account's coupons, and the offers in JLCONE's own coupon list."""
    mine = jlc.api("POST", "/overseas-core-platform/coupons/getCoupons", {"currePage": 1, "pageSize": 100})
    offers = jlc.api("GET", "/overseas-core-platform/coupons/listCouponsForPcOrderAssistant", query={"languageCode": "en"})

    def shape(c: dict) -> dict:
        return {"title": c.get("titleE"), "off": c.get("denomination"), "min_order": c.get("minAmount"),
                "expires": when(c.get("expiryDate")), "status": c.get("couponsStatus")}

    return {"mine": [shape(dict(c.get("couponsRecordVo") or {}, couponsStatus=c.get("couponsStatus")))
                     for c in (mine or {}).get("list") or []],
            "desktop_offers": [shape(c) for c in offers or []]}


def cart(jlc: Session) -> list[dict]:
    """What sits in the cart right now (worth a look before ordering: an old revision's files can linger)."""
    data = jlc.api("GET", "/overseas-core-platform/shoppingCart/showCart", query={"pageNum": 1, "pageSize": 30})
    return (data or {}).get("list") or []


def wallet(jlc: Session) -> dict:
    """The JLC wallet (JPay) balance and its movements."""
    account = jlc.api("GET", "/overseas-core-platform/customer/queryJpayAccountInfo") or {}
    history = jlc.api("POST", "/overseas-core-platform/balance/queryJpayBalance", {"pageNum": 1, "pageSize": 20}) or {}
    return {"balance": account.get("balanceMonay"), "jpay_account": account.get("jpayAccountStatus"),
            "history": history.get("list") or []}
