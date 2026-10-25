"""Native Aravis C API and lossless RGB conversion; no vendor SDK/GenTL imports."""
from __future__ import annotations
import ctypes as C
import os
from pathlib import Path
P, U32, U64, I32 = C.c_void_p, C.c_uint32, C.c_uint64, C.c_int32

class GError(C.Structure):
    _fields_ = [('domain', U32), ('code', I32), ('message', C.c_char_p)]

EP = C.POINTER(C.POINTER(GError))



class GValue(C.Structure):
    _fields_ = [('g_type', C.c_size_t), ('data', U64 * 2)]



def text(value):
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value



def loaded_modules():
    """Read this process's modules to verify no vendor SDK DLL was loaded."""
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], P
    kernel.K32EnumProcessModules.argtypes = [P, C.POINTER(P), U32, C.POINTER(U32)]
    kernel.K32EnumProcessModules.restype = I32
    kernel.K32GetModuleFileNameExW.argtypes = [P, P, C.c_wchar_p, U32]
    kernel.K32GetModuleFileNameExW.restype = U32
    process, handles, needed = kernel.GetCurrentProcess(), (P * 2048)(), U32()
    if not kernel.K32EnumProcessModules(process, handles, C.sizeof(handles), C.byref(needed)):
        raise C.WinError(C.get_last_error())
    if needed.value > C.sizeof(handles):
        raise RuntimeError('Module audit buffer too small')
    paths = []
    for handle in handles[:needed.value // C.sizeof(P)]:
        name = C.create_unicode_buffer(32768)
        if kernel.K32GetModuleFileNameExW(process, handle, name, len(name)):
            paths.append(name.value)
    return paths



class Aravis:
    def __init__(self, dll_dir):
        if os.name != 'nt' or C.sizeof(P) != 8:
            raise RuntimeError('This bundle requires 64-bit Windows Python')
        binary_dir = Path(dll_dir).resolve()
        if not (binary_dir / "libaravis-0.8-0.dll").is_file():
            raise FileNotFoundError("Aravis runtime missing; run Krishnan/tools/setup-aravis.py")
        self.directory = os.add_dll_directory(str(binary_dir))
        # Prevent GenTL discovery through installed vendor SDKs in this child.
        os.environ.pop('GENICAM_GENTL64_PATH', None)
        os.environ.pop('GENICAM_GENTL32_PATH', None)
        self.dll = C.CDLL(str(binary_dir / 'libaravis-0.8-0.dll'))
        self.glib = C.CDLL(str(binary_dir / 'libglib-2.0-0.dll'))
        self.gobject = C.CDLL(str(binary_dir / 'libgobject-2.0-0.dll'))
        def bind(lib, name, result, *args):
            function = getattr(lib, name)
            function.restype, function.argtypes = result, list(args)
            setattr(self, name, function)
        a = self.dll
        bind(a, 'arv_get_n_interfaces', U32)
        bind(a, 'arv_get_interface_id', C.c_char_p, U32)
        for n in ['arv_enable_interface', 'arv_disable_interface']:
            bind(a, n, None, C.c_char_p)
        bind(a, 'arv_update_device_list', None)
        bind(a, 'arv_get_n_devices', U32)
        for n in ['id', 'address', 'vendor', 'model', 'serial_nbr', 'protocol']:
            bind(a, 'arv_get_device_' + n, C.c_char_p, U32)
        bind(a, 'arv_camera_new', P, C.c_char_p, EP)
        bind(a, 'arv_camera_get_device', P, P)
        for n in ['vendor_name', 'model_name', 'device_serial_number', 'device_id', 'pixel_format_as_string']:
            bind(a, 'arv_camera_get_' + n, C.c_char_p, P, EP)
        bind(a, 'arv_camera_get_pixel_format', U32, P, EP)
        bind(a, 'arv_camera_get_payload', U32, P, EP)
        bind(a, 'arv_camera_get_region', None, P, *([C.POINTER(I32)] * 4), EP)
        for n in ['exposure_time', 'gain', 'frame_rate']:
            bind(a, 'arv_camera_get_' + n, C.c_double, P, EP)
        bind(a, 'arv_camera_set_frame_rate', None, P, C.c_double, EP)
        bind(a, 'arv_camera_set_float', None, P, C.c_char_p, C.c_double, EP)
        bind(a, 'arv_camera_get_float', C.c_double, P, C.c_char_p, EP)
        bind(a, 'arv_camera_get_string', C.c_char_p, P, C.c_char_p, EP)
        bind(a, 'arv_camera_set_string', None, P, C.c_char_p, C.c_char_p, EP)
        bind(a, 'arv_camera_get_integer', C.c_int64, P, C.c_char_p, EP)
        bind(a, 'arv_camera_set_integer', None, P, C.c_char_p, C.c_int64, EP)
        bind(a, 'arv_camera_get_integer_bounds', None, P, C.c_char_p, C.POINTER(C.c_int64), C.POINTER(C.c_int64), EP)
        bind(a, 'arv_camera_is_feature_available', I32, P, C.c_char_p, EP)
        bind(a, 'arv_camera_create_stream', P, P, P, P, EP)
        bind(a, 'arv_gv_device_set_packet_size_adjustment', None, P, I32)
        for n in ['start', 'stop']:
            bind(a, 'arv_camera_' + n + '_acquisition', None, P, EP)
        bind(a, 'arv_device_get_genicam_xml', P, P, C.POINTER(C.c_size_t))
        bind(a, 'arv_buffer_new_allocate', P, C.c_size_t)
        bind(a, 'arv_buffer_get_status', I32, P)
        bind(a, 'arv_buffer_get_frame_id', U64, P)
        bind(a, 'arv_buffer_get_timestamp', U64, P)
        bind(a, 'arv_buffer_get_image_data', P, P, C.POINTER(C.c_size_t))
        for n in ['width', 'height']:
            bind(a, 'arv_buffer_get_image_' + n, I32, P)
        bind(a, 'arv_buffer_get_image_pixel_format', U32, P)
        bind(a, 'arv_buffer_get_image_padding', None, P, C.POINTER(I32), C.POINTER(I32))
        bind(a, 'arv_stream_push_buffer', None, P, P)
        bind(a, 'arv_stream_timeout_pop_buffer', P, P, U64)
        bind(a, 'arv_stream_get_statistics', None, P, *([C.POINTER(U64)] * 3))
        bind(a, 'arv_stream_get_n_infos', U32, P)
        bind(a, 'arv_stream_get_info_name', C.c_char_p, P, U32)
        bind(a, 'arv_stream_get_info_type', C.c_size_t, P, U32)
        bind(a, 'arv_stream_get_info_uint64', U64, P, U32)
        bind(a, 'arv_stream_get_info_double', C.c_double, P, U32)
        bind(a, 'arv_shutdown', None)
        bind(self.gobject, 'g_object_unref', None, P)
        bind(self.gobject, 'g_type_from_name', C.c_size_t, C.c_char_p)
        bind(self.gobject, 'g_value_init', P, C.POINTER(GValue), C.c_size_t)
        bind(self.gobject, 'g_value_set_enum', None, C.POINTER(GValue), I32)
        bind(self.gobject, 'g_value_set_int', None, C.POINTER(GValue), I32)
        bind(self.gobject, 'g_value_set_uint', None, C.POINTER(GValue), U32)
        bind(self.gobject, 'g_value_set_double', None, C.POINTER(GValue), C.c_double)
        bind(self.gobject, 'g_value_get_enum', I32, C.POINTER(GValue))
        bind(self.gobject, 'g_value_get_int', I32, C.POINTER(GValue))
        bind(self.gobject, 'g_value_get_uint', U32, C.POINTER(GValue))
        bind(self.gobject, 'g_value_get_double', C.c_double, C.POINTER(GValue))
        bind(self.gobject, 'g_object_set_property', None, P, C.c_char_p, C.POINTER(GValue))
        bind(self.gobject, 'g_object_get_property', None, P, C.c_char_p, C.POINTER(GValue))
        bind(self.gobject, 'g_value_unset', None, C.POINTER(GValue))
        bind(self.glib, 'g_error_free', None, C.POINTER(GError))

    def call(self, name, *args):
        error = C.POINTER(GError)()
        result = getattr(self, name)(*args, C.byref(error))
        if error:
            message = text(error.contents.message)
            code = error.contents.code
            self.g_error_free(error)
            raise RuntimeError(f'{name}: {message} (code {code})')
        return text(result)

    def native_only(self, fake=False):
        target = 'Fake' if fake else 'GigEVision'
        interfaces = [text(self.arv_get_interface_id(i)) for i in range(self.arv_get_n_interfaces())]
        for interface in interfaces:
            getattr(self, 'arv_enable_interface' if interface == target else 'arv_disable_interface')(interface.encode())
        if target not in interfaces:
            raise RuntimeError(f'{target} interface not available: {interfaces}')
        return interfaces

    def discover(self):
        self.arv_update_device_list()
        found = []
        for i in range(self.arv_get_n_devices()):
            found.append({key: text(getattr(self, 'arv_get_device_' + key)(i)) for key in ['id', 'address', 'vendor', 'model', 'serial_nbr', 'protocol']})
        return found

    def stream_property(self, stream, name, value, type_name, enum=False):
        gtype = self.g_type_from_name(type_name.encode())
        if not gtype:
            raise RuntimeError('GObject property type not registered: ' + type_name)
        item = GValue()
        self.g_value_init(C.byref(item), gtype)
        try:
            kind = 'enum' if enum else {'gint':'int','guint':'uint','gdouble':'double'}[type_name]
            getattr(self,'g_value_set_'+kind)(C.byref(item), value)
            self.g_object_set_property(stream, name.encode(), C.byref(item))
            self.g_object_get_property(stream, name.encode(), C.byref(item))
            readback = getattr(self,'g_value_get_'+kind)(C.byref(item))
            if readback != value:
                raise RuntimeError(f'Stream property {name}: requested {value}, read back {readback}')
            return readback
        finally:
            self.g_value_unset(C.byref(item))



def demosaic(raw, pattern):
    """Bilinear Bayer interpolation in RGB order, operating on quarter planes."""
    import numpy as np
    layouts = {'BayerGR8': 'GRBG', 'BayerRG8': 'RGGB', 'BayerGB8': 'GBRG', 'BayerBG8': 'BGGR'}
    layout = layouts[pattern]
    h, w = raw.shape
    padded = np.pad(raw.astype(np.uint16), 1, mode='reflect')
    rgb = np.empty((h,w,3), dtype=np.uint8)
    def sample(y, x, dy, dx):
        return padded[1+y+dy:1+h+dy:2, 1+x+dx:1+w+dx:2]
    for y in range(2):
        for x in range(2):
            measured = layout[2*y+x]
            for channel, color in enumerate('RGB'):
                if measured == color:
                    values = raw[y::2,x::2]
                elif color == 'G':
                    values = (sample(y,x,0,-1)+sample(y,x,0,1)+sample(y,x,-1,0)+sample(y,x,1,0)) // 4
                else:
                    cy,cx = divmod(layout.index(color),2)
                    if y == cy:
                        values = (sample(y,x,0,-1)+sample(y,x,0,1)) // 2
                    elif x == cx:
                        values = (sample(y,x,-1,0)+sample(y,x,1,0)) // 2
                    else:
                        values = (sample(y,x,-1,-1)+sample(y,x,-1,1)+sample(y,x,1,-1)+sample(y,x,1,1)) // 4
                rgb[y::2,x::2,channel] = values
    return rgb



def frame_to_rgb(frame, orientation='sensor'):
    import numpy as np
    meta, data = frame
    h, w, padding = meta['height'], meta['width'], meta['x_padding']
    if meta['pixel_format_name'] == 'Mono8' or meta['pixel_format_name'].startswith('Bayer'):
        raw = np.frombuffer(data, dtype=np.uint8, count=h*(w+padding)).reshape(h, w + padding)[:, :w]
        rgb = demosaic(raw, meta['pixel_format_name']) if meta['pixel_format_name'].startswith('Bayer') else np.repeat(raw[:, :, None], 3, axis=2)
    elif meta['pixel_format_name'] in ['RGB8', 'RGB8Packed']:
        rgb = np.frombuffer(data, dtype=np.uint8, count=h*(w*3+padding)).reshape(h, w * 3 + padding)[:, :w*3].reshape(h,w,3)
    else:
        raise RuntimeError('Unsupported image conversion: ' + meta['pixel_format_name'])
    # The supplied IMI TIFF uses the opposite scan-line order. Flip AFTER
    # demosaicing, so Bayer phase and channel order remain correct.
    return np.ascontiguousarray(rgb[::-1]) if orientation == 'reference' else rgb


