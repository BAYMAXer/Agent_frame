"""Unicode keyboard input for GUI paths/names; never uses clipboard or files."""
import ctypes


class KeyboardInput(ctypes.Structure):
    _fields_ = [('vk', ctypes.c_uint16), ('scan', ctypes.c_uint16),
                ('flags', ctypes.c_uint32), ('time', ctypes.c_uint32), ('extra', ctypes.c_size_t)]


class MouseInput(ctypes.Structure):
    _fields_ = [('dx', ctypes.c_int32), ('dy', ctypes.c_int32), ('data', ctypes.c_uint32),
                ('flags', ctypes.c_uint32), ('time', ctypes.c_uint32), ('extra', ctypes.c_size_t)]


class InputUnion(ctypes.Union):
    _fields_ = [('keyboard', KeyboardInput), ('mouse', MouseInput)]


class Input(ctypes.Structure):
    _anonymous_ = ('value',)
    _fields_ = [('type', ctypes.c_uint32), ('value', InputUnion)]


def unicode_events(text):
    data = str(text).replace('\r\n', '\n').encode('utf-16-le')
    events = []
    for i in range(0, len(data), 2):
        code = int.from_bytes(data[i:i + 2], 'little')
        vk, scan, flags = (13, 0, 0) if code in (10, 13) else (9, 0, 0) if code == 9 else (0, code, 4)
        for up in (0, 2):
            event = Input(type=1)
            event.keyboard = KeyboardInput(vk, scan, flags | up, 0, 0)
            events.append(event)
    return events


def type_unicode(text):
    import pyautogui
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.SendInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(Input), ctypes.c_int]
    user.SendInput.restype = ctypes.c_uint32
    events = unicode_events(text)
    for start in range(0, len(events), 128):
        pyautogui.failSafeCheck()
        batch = events[start:start + 128]
        inputs = (Input * len(batch))(*batch)
        if user.SendInput(len(inputs), inputs, ctypes.sizeof(Input)) != len(inputs):
            raise OSError('Windows 未确认完整文本输入；请重新观察输入框，勿盲目重放')
