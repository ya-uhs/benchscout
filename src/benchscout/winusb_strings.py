"""The USB string descriptors (manufacturer, product, serial) of every device, on Windows.

Windows keeps no copy of a device's manufacturer string, and the names WMI gives are its drivers' ("USB Composite
Device", "(Standard USB Host Controller)"), not what the device calls itself. So each device's string descriptors
are read through its hub, the way Microsoft's USBView sample does (IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION).
That is a standard GET_DESCRIPTOR request the hub sends; it opens nothing and changes nothing on the device, and
works whether or not the device has a driver.
"""

from __future__ import annotations

import ctypes
import sys

GUID_DEVINTERFACE_USB_HUB = "{f18a0e88-c30c-11d0-8815-00a0c906bed8}"
IOCTL_USB_GET_NODE_INFORMATION = 0x220408
IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION = 0x220410
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX = 0x220448
DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x02, 0x10
GENERIC_WRITE, FILE_SHARE_WRITE, OPEN_EXISTING = 0x40000000, 0x02, 3
INVALID_HANDLE = ctypes.c_void_p(-1).value


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("InterfaceClassGuid", _GUID), ("Flags", ctypes.c_uint32),
                ("Reserved", ctypes.c_size_t)]


def _guid(text: str) -> _GUID:
    import uuid

    u = uuid.UUID(text)
    g = _GUID()
    g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
    g.Data4[:] = list(u.bytes[8:])
    return g


def _hub_paths() -> list[str]:
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)  # type: ignore[attr-defined]
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32]
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.c_uint32,
                                                     ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
                                                          ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    guid = _guid(GUID_DEVINTERFACE_USB_HUB)
    devs = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not devs or devs == INVALID_HANDLE:
        return []
    paths = []
    try:
        i = 0
        while True:
            data = _SP_DEVICE_INTERFACE_DATA()
            data.cbSize = ctypes.sizeof(data)
            if not setupapi.SetupDiEnumDeviceInterfaces(devs, None, ctypes.byref(guid), i, ctypes.byref(data)):
                break
            i += 1
            need = ctypes.c_uint32(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(data), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize: 8 on 64-bit Windows, 6 on 32-bit
            ctypes.c_uint32.from_buffer(buf).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(data), buf, need, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devs)
    return paths


def _ioctl(kernel32, handle, code: int, buf) -> int | None:
    got = ctypes.c_uint32(0)
    ok = kernel32.DeviceIoControl(handle, code, buf, len(buf), buf, len(buf), ctypes.byref(got), None)
    return got.value if ok else None


def _string(kernel32, hub, port: int, index: int) -> str | None:
    if not index:
        return None
    req = ctypes.create_string_buffer(12 + 255)
    ctypes.c_uint32.from_buffer(req, 0).value = port
    ctypes.c_uint16.from_buffer(req, 4).value = 0x0680  # bmRequestType 0x80 (device to host), bRequest 6 (GET_DESCRIPTOR)
    ctypes.c_uint16.from_buffer(req, 6).value = (0x03 << 8) | index  # string descriptor `index`
    ctypes.c_uint16.from_buffer(req, 8).value = 0x0409  # US English, what USBView asks for
    ctypes.c_uint16.from_buffer(req, 10).value = 255
    if _ioctl(kernel32, hub, IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION, req) is None:
        return None
    return clean(req.raw[12:])


def clean(descriptor: bytes) -> str | None:
    """A string descriptor's text: bLength bytes, from the third on, UTF-16LE, up to the first NUL (some EEPROMs
    give a bLength that runs past the text: "FactoryAIOT Pro", a NUL, then stray bytes, on a Tang Nano's FT2232)."""
    if len(descriptor) < 2 or descriptor[1] != 0x03 or descriptor[0] < 2:
        return None
    text = descriptor[2:descriptor[0]].decode("utf-16-le", errors="replace")
    return text.split("\0", 1)[0].strip() or None


def usb_strings() -> list[dict]:
    """[{vid, pid, manufacturer, product, serial}] for each device on every hub port; [] off Windows or on failure."""
    if sys.platform != "win32":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.c_uint32, ctypes.c_void_p]
    kernel32.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    found = []
    for path in _hub_paths():
        hub = kernel32.CreateFileW(path, GENERIC_WRITE, FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
        if not hub or hub == INVALID_HANDLE:
            continue
        try:
            node = ctypes.create_string_buffer(76)  # USB_NODE_INFORMATION (packed): NodeType, then the hub descriptor
            if _ioctl(kernel32, hub, IOCTL_USB_GET_NODE_INFORMATION, node) is None:
                continue
            for port in range(1, node.raw[6] + 1):  # bNumberOfPorts
                info = ctypes.create_string_buffer(512)  # USB_NODE_CONNECTION_INFORMATION_EX (packed)
                ctypes.c_uint32.from_buffer(info, 0).value = port
                if _ioctl(kernel32, hub, IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX, info) is None:
                    continue
                raw = info.raw
                if int.from_bytes(raw[31:35], "little") != 1 or raw[24]:  # not DeviceConnected, or a hub
                    continue
                vid, pid = int.from_bytes(raw[12:14], "little"), int.from_bytes(raw[14:16], "little")
                found.append({"vid": vid, "pid": pid, "manufacturer": _string(kernel32, hub, port, raw[18]),
                              "product": _string(kernel32, hub, port, raw[19]), "serial": _string(kernel32, hub, port, raw[20])})
        finally:
            kernel32.CloseHandle(hub)
    return found


if __name__ == "__main__":  # python -m benchscout.winusb_strings: what this reads, for checking by eye
    import json

    print(json.dumps(usb_strings(), indent=1))
