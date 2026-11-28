"""SSDP response enrichment and TV classification, with bounded parallel HTTP checks."""
import threading


def discover(timeout, ssdp_search, fetch_description, parse_tv_description, xml_text,
             port_open, adb_available, adb_port, log, workers=8):
    """Return every discovered TV; parallelize description requests without dropping late results."""
    try:
        locations = ssdp_search(timeout)
    except Exception as exc:
        log(f"SSDP search failed: {exc}")
        return []

    items = list(locations.items())
    found = {}
    next_index = [0]
    index_lock = threading.Lock()
    found_lock = threading.Lock()

    def check(ip, device_locations):
        cast = None
        for location in device_locations:
            xml = fetch_description(ip, location)
            info = parse_tv_description(xml) if xml else None
            if info:
                with found_lock:
                    found[ip] = {"ip": ip, "name": info[0], "model": info[1], "kind": "sony"}
                return
            if xml:
                if cast is None and "dial-multiscreen-org" in xml:
                    cast = (xml_text(xml, "friendlyName"), xml_text(xml, "modelName"),
                            xml_text(xml, "manufacturer"))
                log(f"SSDP: {ip} {location}: not a Bravia (manufacturer={xml_text(xml, 'manufacturer') or '?'}, "
                    f"model={xml_text(xml, 'modelName') or '?'}, scalar-api={'ScalarWebAPI' in xml})")
        if cast and adb_available and port_open(ip, adb_port):
            with found_lock:
                found[ip] = {"ip": ip, "name": cast[0], "model": cast[1] or cast[2], "kind": "adb"}

    def worker():
        while True:
            with index_lock:
                if next_index[0] >= len(items):
                    return
                item = items[next_index[0]]
                next_index[0] += 1
            try:
                check(*item)
            except Exception as exc:
                log(f"SSDP: checking {item[0]} failed: {exc}")

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(max(1, workers), len(items)))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    tvs = list(found.values())
    tvs.sort(key=lambda tv: ("bravia" not in (tv["name"] + tv["model"]).lower(), tv["ip"]))
    log(f"SSDP: {len(locations)} device(s) answered, {len(tvs)} TV(s) found")
    return tvs
