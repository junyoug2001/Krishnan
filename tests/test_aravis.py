"""Aravis color, continuous draining, rollback, and watchdog checks."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch,Mock

import numpy as np
from krishnan.aravis_native import demosaic,frame_to_rgb
from krishnan.aravis_sdk import AravisSDKCamera
from krishnan.settings import load_settings


class ColorChecks(unittest.TestCase):
    def test_all_bayer_phases_preserve_known_rgb_and_neutral_patches(self):
        colors=np.array([[220,20,10],[10,210,30],[15,25,200],[100,100,100]],dtype=np.uint8)
        positions={'BayerGR8':(1,0,2,1),'BayerRG8':(0,1,1,2),'BayerGB8':(1,2,0,1),'BayerBG8':(2,1,1,0)}
        for name,channels in positions.items():
            mosaic=np.empty((64,64),dtype=np.uint8)
            for i,color in enumerate(colors):
                y,x=(i//2)*32,(i%2)*32
                for j,c in enumerate(channels):mosaic[y+j//2:y+32:2,x+j%2:x+32:2]=color[c]
            rgb=demosaic(mosaic,name)
            for i,color in enumerate(colors):
                y,x=(i//2)*32,(i%2)*32
                self.assertTrue(np.all(rgb[y+3:y+29,x+3:x+29]==color),name)

    def test_scan_line_orientation_does_not_change_bayer_phase_or_raw_bytes(self):
        raw=np.arange(16*16,dtype=np.uint8).reshape(16,16)
        data=raw.tobytes()
        meta=dict(width=16,height=16,x_padding=0,pixel_format_name='BayerGR8')
        sensor=frame_to_rgb((meta,data),'sensor')
        reference=frame_to_rgb((meta,data),'reference')
        np.testing.assert_array_equal(reference,sensor[::-1])
        self.assertEqual(data,raw.tobytes())
        np.testing.assert_array_equal(sensor[::2,1::2,0],raw[::2,1::2])
        np.testing.assert_array_equal(sensor[1::2,::2,2],raw[1::2,::2])

    def test_row_padding_does_not_become_image_pixels(self):
        meta=dict(width=2,height=2,x_padding=2,pixel_format_name='Mono8')
        data=bytes([1,2,255,255,3,4,255,255])
        rgb=frame_to_rgb((meta,data))
        np.testing.assert_array_equal(rgb[:,:,0],[[1,2],[3,4]])


class AcquisitionChecks(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Native Windows Aravis bundle')
    def test_native_fake_stream_keeps_draining_when_preview_is_slower(self):
        config=load_settings();config.update(aravis_manage_network=False,aravis_warmup_s=0)
        camera=AravisSDKCamera(config,fake=True)
        try:
            camera.open();camera.start()
            first=camera.read_frame(timeout_s=2)
            time.sleep(.55)
            second=camera.read_frame(timeout_s=2)
            stats=camera.statistics()
            self.assertEqual(first[:2],(512,512))
            self.assertNotEqual(first[2],second[2])
            self.assertGreater(stats['rgb_frames'],5)
            self.assertGreater(stats['preview_skipped'],2)
            self.assertEqual(stats['incomplete'],0)
        finally:camera.close()
        self.assertIsNone(camera.stream)
        self.assertIsNone(camera.camera)
        self.assertFalse(camera.initialized)

    def test_missing_runtime_releases_the_already_prepared_network_session(self):
        config=load_settings()
        network=Mock();network.process=None
        with patch('krishnan.aravis_sdk.CameraNetworkSession',return_value=network),patch('krishnan.aravis_sdk.Aravis',side_effect=FileNotFoundError('no runtime')):
            camera=AravisSDKCamera(config)
            with self.assertRaisesRegex(FileNotFoundError,'no runtime'):camera.open()
        network.open.assert_called_once()
        network.close.assert_called_once()


# These functions replace EVERY network mutation/query in the broker. No real
# adapter, binding or firewall cmdlet is called by these offline tests.
POWERSHELL_STUBS=r'''
$script:bound=$true; $script:rule=$false
function Get-NetIPAddress { [CmdletBinding()]param($AddressFamily,$IPAddress,$InterfaceIndex) [pscustomobject]@{InterfaceAlias='OfflineTest';IPAddress=$IPAddress} }
function Get-NetAdapter { [CmdletBinding()]param($Name) [pscustomobject]@{Name='OfflineTest';Status='Up';ifIndex=99;InterfaceGuid='offline-test'} }
function Get-NetAdapterBinding { [CmdletBinding()]param($Name,$ComponentID) if ($env:ARAVIS_TEST_NO_LEGACY -ne '1' -and (-not $ComponentID -or $ComponentID -eq 'neptunegev')) {[pscustomobject]@{ComponentID='neptunegev';Enabled=$script:bound}};if (-not $ComponentID) {[pscustomobject]@{ComponentID='ms_tcpip';Enabled=$true}} }
function Disable-NetAdapterBinding { [CmdletBinding(SupportsShouldProcess=$true)]param($Name,$ComponentID) $script:bound=$false }
function Enable-NetAdapterBinding { [CmdletBinding(SupportsShouldProcess=$true)]param($Name,$ComponentID) $script:bound=$true }
function New-NetFirewallRule { [CmdletBinding()]param($Name,$DisplayName,$Direction,$Action,$Protocol,$Program,$RemoteAddress,$LocalAddress,$InterfaceAlias,$Profile) if ($env:ARAVIS_TEST_FAIL_RULE -eq '1') {throw 'simulated rule creation failure'};$script:rule=$true }
function Get-NetFirewallRule { [CmdletBinding()]param($Name) if ($script:rule) {[pscustomobject]@{Name=$Name}} }
function Remove-NetFirewallRule { [CmdletBinding()]param([Parameter(ValueFromPipeline=$true)]$InputObject) process {$script:rule=$false} }
'''
POWERSHELL_STUBS=POWERSHELL_STUBS.replace('$script:bound','$global:aravisTestBound').replace('$script:rule','$global:aravisTestRule')


@unittest.skipUnless(os.name=='nt','Windows PowerShell watchdog')
class NetworkWatchdogChecks(unittest.TestCase):
    def exercise(self, *, fail_rule=False, owner_loss=False, no_legacy=False):
        from krishnan import aravis_network
        helper=Path(aravis_network.__file__).with_suffix('.ps1')
        powershell=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            wrapper=folder/'offline.ps1'
            wrapper.write_text(POWERSHELL_STUBS+"\n& ($args[0]) -OwnerPid ($args[1]) -StateDirectory ($args[2]) -Program offline-python -HostAddress 169.254.200.199 -CameraAddress 169.254.200.198\n",encoding='utf-8-sig')
            owner=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],creationflags=subprocess.CREATE_NO_WINDOW)
            env=dict(os.environ,ARAVIS_TEST_FAIL_RULE='1' if fail_rule else '0',ARAVIS_TEST_NO_LEGACY='1' if no_legacy else '0')
            process=subprocess.Popen([str(powershell),'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(wrapper),str(helper),str(owner.pid),str(folder)],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                deadline=time.monotonic()+12
                while time.monotonic()<deadline:
                    try:state=json.loads((folder/'state.json').read_text(encoding='utf-8-sig'))
                    except (OSError,ValueError):state={}
                    if state.get('Ready') or state.get('Closed'):break
                    time.sleep(.03)
                if not fail_rule:self.assertTrue(state.get('Ready'),state)
                if owner_loss:
                    owner.terminate()  # Only the offline sleeping test owner.
                    owner.wait(3)
                else:(folder/'stop').touch()
                output=process.communicate(timeout=12)[0].decode(errors='replace')
                state=json.loads((folder/'state.json').read_text(encoding='utf-8-sig'))
                self.assertTrue(state['Closed'],output)
                if no_legacy:self.assertFalse(state['BindingAttempted'],output)
                else:self.assertTrue(state['BindingRestored'],output)
                self.assertEqual(state['BindingDifferences'],[])
                self.assertEqual(state['CleanupErrors'],[])
                self.assertTrue(state['RuleRemoved'])
                self.assertEqual(bool(state.get('Error')),fail_rule)
            finally:
                if owner.poll() is None:owner.terminate();owner.wait(3)
                if process.poll() is None:
                    (folder/'stop').touch();process.wait(12)

    def test_normal_session_restores_binding_and_removes_rule(self):self.exercise()
    def test_rule_creation_failure_rolls_back_the_disabled_filter(self):self.exercise(fail_rule=True)
    def test_dead_backend_owner_triggers_independent_restoration(self):self.exercise(owner_loss=True)
    def test_no_legacy_driver_is_required(self):self.exercise(no_legacy=True)


if __name__=='__main__':unittest.main()
