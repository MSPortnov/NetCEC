"""USB wake source discovery and configuration through sysfs."""
import os


def read(path):
    try:
        with open(path) as stream:
            return stream.read().strip()
    except Exception:
        return ""


def devices(usb_path, read_value=read):
    found = {}
    try:
        names = sorted(os.listdir(usb_path))
    except Exception:
        return []
    for name in names:
        if ":" in name:
            continue
        device = os.path.join(usb_path, name)
        wake_path = os.path.join(device, "power", "wakeup")
        vendor = read_value(os.path.join(device, "idVendor"))
        product = read_value(os.path.join(device, "idProduct"))
        if not os.path.exists(wake_path) or not vendor:
            continue
        device_class = read_value(os.path.join(device, "bDeviceClass"))
        if device_class == "09":
            continue
        key = f"{vendor}:{product}"
        label = " ".join(value for value in (
            read_value(os.path.join(device, "manufacturer")),
            read_value(os.path.join(device, "product")),
        ) if value) or key
        entry = found.setdefault(key, {"id": key, "name": label, "wake": False,
                                       "bt": device_class == "e0", "paths": []})
        entry["paths"].append(wake_path)
        if read_value(wake_path) == "enabled":
            entry["wake"] = True
    return sorted(found.values(), key=lambda item: (not item["bt"], item["name"].lower()))


def set_device(dev_id, enabled, list_devices, read_value=read):
    count = 0
    wanted = "enabled" if enabled else "disabled"
    for device in list_devices():
        if device["id"] != dev_id:
            continue
        for path in device["paths"]:
            if read_value(path) == wanted:
                count += 1
                continue
            try:
                with open(path, "w") as stream:
                    stream.write(wanted)
                count += 1
            except Exception:
                pass
    return count
