"""IMC-720G acquisition through native Aravis GigE Vision sockets."""
from __future__ import annotations

import ctypes as C
import math
from collections import Counter
import threading
import time

from .aravis_native import Aravis, I32, U64, frame_to_rgb, loaded_modules
from .aravis_network import CameraNetworkSession


class AravisSDKCamera:
    """A continuously drained stream; GUI preview never owns native buffers."""

    def __init__(self, config, *, fake=False):
        self.config, self.fake = config, fake
        self.api = self.camera = self.stream = self.network = None
        self.initialized = self.acquiring = False
        self.details = {}
        self._restore = []
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._latest = None
        self._last_delivered = 0
        self._error = None
        self._stats = {}
        self._statuses = Counter()
        self._count = self._missing = self._timeouts = self._preview_skips = 0
        self._first_at = self._last_at = 0.0
        self._previous_id = None

    def open(self):
        if self.initialized:
            return self.details
        try:
            if not self.fake and self.config['aravis_manage_network']:
                self.network = CameraNetworkSession(self.config['aravis_host_ip'], self.config['aravis_ip'])
                self.network.open()
            self.api = api = Aravis(self.config['aravis_dll_dir'])
            api.native_only(self.fake)
            self.camera = api.call('arv_camera_new', b'Fake_1' if self.fake else self.config['aravis_ip'].encode())
            if not self.camera:
                raise RuntimeError('Aravis returned no camera')
            identity = {n: api.call('arv_camera_get_'+n,self.camera) for n in ['model_name','device_serial_number']}
            if not self.fake and (identity['model_name'] != 'IMC-720G' or identity['device_serial_number'] != self.config['aravis_serial']):
                raise RuntimeError('Aravis camera identity mismatch: '+str(identity))
            region = [I32() for _ in range(4)]
            api.call('arv_camera_get_region',self.camera,*[C.byref(x) for x in region])
            self.width,self.height = region[2].value,region[3].value
            self.pixel_format = api.call('arv_camera_get_pixel_format',self.camera)
            self.pixel_name = api.call('arv_camera_get_pixel_format_as_string',self.camera)
            if self.pixel_name not in ['BayerGR8','BayerRG8','BayerGB8','BayerBG8','Mono8','RGB8','RGB8Packed']:
                raise RuntimeError('Unsupported Aravis pixel format: '+self.pixel_name)
            self.payload = api.call('arv_camera_get_payload',self.camera)
            if not 0 < self.payload <= 128*1024*1024 or not 0 < self.width <= 10000 or not 0 < self.height <= 10000:
                raise RuntimeError('Invalid Aravis image geometry/payload')
            self.details = {'transport':'Aravis native GigE Vision','model':identity['model_name'],
                            'serial':identity['device_serial_number'],'width':self.width,'height':self.height,
                            'pixel_format':self.pixel_name,'bayer_layout':self.pixel_name,
                            'bayer_conversion':'bilinear RGB; reference scan-line orientation',
                            'exposure_us':api.call('arv_camera_get_exposure_time',self.camera),
                            'gain':api.call('arv_camera_get_gain',self.camera)}
            self.initialized = True
            return self.details
        except BaseException:
            self.close()
            raise

    def _temporary(self, kind, feature, desired):
        api,camera = self.api,self.camera
        if kind == 'rate':
            original = api.call('arv_camera_get_frame_rate',camera)
        else:
            original = api.call('arv_camera_get_'+kind,camera,feature.encode())
        if original == desired:
            return
        self._restore.append((kind,feature,original))
        if kind == 'rate': api.call('arv_camera_set_frame_rate',camera,desired)
        else: api.call('arv_camera_set_'+kind,camera,feature.encode(),desired.encode() if kind=='string' else desired)
        if kind == 'rate': actual=api.call('arv_camera_get_frame_rate',camera)
        else: actual=api.call('arv_camera_get_'+kind,camera,feature.encode())
        matches = math.isclose(actual,desired,rel_tol=1e-4,abs_tol=1e-6) if kind in ['rate','float'] else actual==desired
        if not matches:
            raise RuntimeError(feature+' setting readback mismatch')

    def start(self):
        if not self.initialized:
            raise RuntimeError('Open the Aravis camera first')
        if self.acquiring:
            return
        api,camera = self.api,self.camera
        try:
            self._temporary('string','AcquisitionMode','Continuous')
            self._temporary('string','TriggerMode','Off')
            if self.fake:
                self._temporary('rate','frame_rate',self.config['aravis_frame_rate'])
            else:
                # Generic set_frame_rate also changes trigger selectors; use
                # the IMI feature directly and preserve unrelated controls.
                self._temporary('float','AcquisitionFrameRateAbs',self.config['aravis_frame_rate'])
                frequency=api.call('arv_camera_get_integer',camera,b'GevTimestampTickFrequency')
                if frequency <= 0: raise RuntimeError('Camera timestamp frequency is invalid')
                ticks=round(self.config['aravis_packet_delay_us']*frequency/1_000_000)
                low,high=C.c_int64(),C.c_int64()
                api.call('arv_camera_get_integer_bounds',camera,b'GevSCPD',C.byref(low),C.byref(high))
                if not low.value <= ticks <= high.value: raise ValueError('Packet delay outside camera limits')
                self._temporary('integer','GevSCPD',ticks)
                api.arv_gv_device_set_packet_size_adjustment(api.arv_camera_get_device(camera),0)
                for feature in ['GevSCDA','GevSCPHostPort','GevSCPSPacketSize']:
                    self._restore.append(('integer',feature,api.call('arv_camera_get_integer',camera,feature.encode())))
            self.stream=api.call('arv_camera_create_stream',camera,None,None)
            if not self.stream: raise RuntimeError('Aravis returned no stream')
            if not self.fake:
                for name,value,kind,enum in [('socket-buffer',0,'ArvGvStreamSocketBuffer',True),
                    ('socket-buffer-size',32*1024*1024,'gint',False),
                    ('packet-request-ratio',1.0,'gdouble',False),('frame-retention',300_000,'guint',False),
                    ('packet-timeout',5_000,'guint',False)]:
                    api.stream_property(self.stream,name,value,kind,enum)
            for _ in range(16):
                buffer=api.arv_buffer_new_allocate(self.payload)
                if not buffer: raise MemoryError('Aravis buffer allocation failed')
                api.arv_stream_push_buffer(self.stream,buffer)
            api.call('arv_camera_start_acquisition',camera)
            self.acquiring=True
            self.details['frame_rate_readback']=api.call('arv_camera_get_frame_rate',camera)
            # Warm up in the original balancing mode before freezing gains.
            warmup=time.monotonic()+self.config['aravis_warmup_s']
            while time.monotonic()<warmup:
                self._pop_frame(200_000,decode=False)
            if not self.fake and self.config['aravis_freeze_white_balance']:
                if not api.call('arv_camera_is_feature_available',camera,b'BalanceWhiteAuto'):
                    raise RuntimeError('Camera white-balance mode is unavailable')
                self._temporary('string','BalanceWhiteAuto','Off')
            self._count=self._missing=self._timeouts=self._preview_skips=0
            self._statuses.clear();self._previous_id=None;self._latest=None;self._last_delivered=0
            self._first_at=self._last_at=0.0;self._error=None;self._stop.clear()
            self._thread=threading.Thread(target=self._receive,name='aravis-rgb',daemon=True)
            self._thread.start()
        except BaseException:
            self.close()
            raise

    def _pop_frame(self, timeout_us, *, decode=True):
        api=self.api
        buffer=api.arv_stream_timeout_pop_buffer(self.stream,timeout_us)
        if not buffer: return None
        try:
            status=api.arv_buffer_get_status(buffer)
            self._statuses[str(status)]+=1
            if status!=0:
                return ('incomplete',status)
            if not decode: return ('warmup',None)
            count,xp,yp=C.c_size_t(),I32(),I32()
            pointer=api.arv_buffer_get_image_data(buffer,C.byref(count))
            api.arv_buffer_get_image_padding(buffer,C.byref(xp),C.byref(yp))
            width,height=api.arv_buffer_get_image_width(buffer),api.arv_buffer_get_image_height(buffer)
            if not pointer or not 0<count.value<=self.payload or (width,height,api.arv_buffer_get_image_pixel_format(buffer))!=(self.width,self.height,self.pixel_format):
                raise RuntimeError('Aravis returned invalid image data')
            meta={'width':width,'height':height,'x_padding':xp.value,'y_padding':yp.value,'pixel_format_name':self.pixel_name}
            rgb=frame_to_rgb((meta,C.string_at(pointer,count.value)),'sensor' if self.fake else 'reference')
            return ('frame',(api.arv_buffer_get_frame_id(buffer),width,height,rgb.tobytes()))
        finally:
            api.arv_stream_push_buffer(self.stream,buffer)

    def _receive(self):
        try:
            idle_since=time.monotonic()
            while not self._stop.is_set():
                result=self._pop_frame(200_000)
                if result is None:
                    if time.monotonic()-idle_since>2:
                        self._timeouts+=1
                        raise TimeoutError('No Aravis image for two seconds; check camera Ethernet/firewall')
                    continue
                idle_since=time.monotonic()
                if result[0]!='frame':
                    continue
                frame_id,width,height,pixels=result[1]
                now=time.perf_counter()
                with self._condition:
                    if self._previous_id is not None:
                        increment=frame_id-self._previous_id
                        if increment<0 and not self.fake: increment+=65535
                        self._missing+=max(0,increment-1)
                    self._previous_id=frame_id
                    self._count+=1
                    if not self._first_at:self._first_at=now
                    self._last_at=now
                    self._latest=(self._count,width,height,pixels)
                    self._condition.notify_all()
        except BaseException as exc:
            with self._condition:
                self._error=exc
                self._condition.notify_all()

    def read_frame(self,timeout_s=1.0):
        deadline=time.monotonic()+timeout_s
        with self._condition:
            while self._latest is None or self._latest[0]<=self._last_delivered:
                if self._error: raise RuntimeError(str(self._error)) from self._error
                if not self.acquiring: raise RuntimeError('Aravis acquisition is stopped')
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('Waiting for a new Aravis frame')
                self._condition.wait(remaining)
            sequence,width,height,pixels=self._latest
            self._preview_skips+=max(0,sequence-self._last_delivered-1)
            self._last_delivered=sequence
            return width,height,pixels

    def statistics(self):
        stats=dict(self._stats)
        if self.stream:
            counters=[U64() for _ in range(3)]
            self.api.arv_stream_get_statistics(self.stream,*[C.byref(x) for x in counters])
            stats.update(completed=counters[0].value,incomplete=counters[1].value,underruns=counters[2].value)
            stats['stream_info']={}
            for i in range(self.api.arv_stream_get_n_infos(self.stream)):
                name=self.api.arv_stream_get_info_name(self.stream,i).decode('utf-8')
                kind=self.api.arv_stream_get_info_type(self.stream,i)
                stats['stream_info'][name]=self.api.arv_stream_get_info_double(self.stream,i) if kind==self.api.g_type_from_name(b'gdouble') else self.api.arv_stream_get_info_uint64(self.stream,i)
        with self._condition:
            elapsed=self._last_at-self._first_at
            stats.update(rgb_frames=self._count,rgb_fps=(self._count-1)/elapsed if elapsed>0 else 0,
                         missing_ids=self._missing,image_timeouts=self._timeouts,preview_skipped=self._preview_skips,
                         statuses=dict(self._statuses))
        return stats

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3)
            if self._thread.is_alive(): raise RuntimeError('Aravis receive thread has not stopped; buffers retained')
            self._thread=None
        if self.acquiring:
            self.api.call('arv_camera_stop_acquisition',self.camera)
            self.acquiring=False
        with self._condition:self._condition.notify_all()

    def close(self):
        errors=[]
        try:
            self.stop()
        except Exception:
            # A live native reader must retain its buffers until it can stop.
            if self._thread and self._thread.is_alive(): raise
            errors.append('Acquisition stop failed')
        if self.api:
            self._stats=self.statistics()
            if self.stream:
                self.api.g_object_unref(self.stream);self.stream=None
            for kind,feature,original in reversed(self._restore):
                try:
                    if kind=='rate':
                        self.api.call('arv_camera_set_frame_rate',self.camera,original)
                        actual=self.api.call('arv_camera_get_frame_rate',self.camera)
                    else:
                        self.api.call('arv_camera_set_'+kind,self.camera,feature.encode(),original.encode() if kind=='string' else original)
                        actual=self.api.call('arv_camera_get_'+kind,self.camera,feature.encode())
                    matches=math.isclose(actual,original,rel_tol=1e-4,abs_tol=1e-6) if kind in ['rate','float'] else actual==original
                    if not matches:raise RuntimeError(feature+' restoration readback mismatch')
                except Exception as exc:errors.append(str(exc))
            self._restore.clear()
            if self.camera:self.api.g_object_unref(self.camera);self.camera=None
            self.api.arv_shutdown()
            self.api.directory.close();self.api=None
            vendor=[p for p in loaded_modules() if any(s in p.lower() for s in ['neptune','shamrock','thorlabs','andor'])]
            if vendor:errors.append('Unexpected vendor SDK module in Aravis process: '+str(vendor))
        self.initialized=self.acquiring=False
        if self.network:
            try:self.network.close()
            except Exception as exc:errors.append(str(exc))
            if self.network.process is None:self.network=None
        if errors:raise RuntimeError('; '.join(errors))
