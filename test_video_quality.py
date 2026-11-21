import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.video_quality import VideoQuality
from backend.sdk_encoder import SDKEncoder, SDKError

CAP = '''<AudioVideoCompressInfo><VideoCompressInfo><ChannelList><ChannelEntry><ChannelNumber>1</ChannelNumber><MainChannel><VideoResolutionList>
<VideoResolutionEntry><Index>86</Index><VideoFrameRate>9,14,17</VideoFrameRate><VideoBitrate><Range>13,19,23</Range></VideoBitrate></VideoResolutionEntry>
<VideoResolutionEntry><Index>19</Index><VideoFrameRate>9,14,17</VideoFrameRate><VideoBitrate><Range>13,19,23</Range></VideoBitrate></VideoResolutionEntry>
<VideoResolutionEntry><Index>27</Index><VideoFrameRate>9,14,17</VideoFrameRate><VideoBitrate><Range>13,19,23</Range></VideoBitrate></VideoResolutionEntry>
</VideoResolutionList></MainChannel></ChannelEntry></ChannelList></VideoCompressInfo></AudioVideoCompressInfo>'''


class EncoderTests(unittest.TestCase):
    def make_encoder(self):
        encoder=SDKEncoder()
        encoder.fixture=dict(ok=True,source='hcnetsdk',resolution_code=19,fps_code=14,kbps=1024,codec=1,capabilities_xml=CAP)
        encoder.requests=[]
        def call(*args):
            encoder.requests.append(args)
            if args[0]=='set':
                encoder.fixture.update(resolution_code=args[1],fps_code=args[2],kbps=args[3])
            return dict(encoder.fixture)
        encoder.call=call
        return encoder

    def test_native_sdk_codes_and_readback(self):
        encoder=self.make_encoder()
        desired=dict(width=640,height=360,fps=8,kbps=384)
        self.assertEqual(encoder.apply(desired),desired)
        self.assertEqual(encoder.requests[-1],('set',86,9,384))
        self.assertEqual(encoder.fixture['codec'],1)

    def test_unsupported_parameter_never_sets(self):
        encoder=self.make_encoder()
        with self.assertRaises(ValueError): encoder.apply(dict(width=800,height=600,fps=8,kbps=384))
        self.assertTrue(all(request[0]!='set' for request in encoder.requests))

    def test_full_frame_rate_is_not_guessed_to_25(self):
        encoder=self.make_encoder()
        encoder.fixture['fps_code']=0
        self.assertEqual(encoder.inspect()[0]['fps'],0)

    def test_auto_profile_fits_supported_fps_without_exceeding_ceiling(self):
        encoder=self.make_encoder()
        encoder.fixture['capabilities_xml']=CAP.replace('9,14,17','26,14,17')
        encoder.inspect()
        target=encoder.adapt(dict(width=640,height=360,fps=8,kbps=500))
        self.assertEqual(target['fps'],5)
        self.assertEqual(target['kbps'],384)

    def test_substream_cannot_expand_main_capabilities(self):
        encoder=self.make_encoder()
        encoder.fixture['capabilities_xml']=CAP.replace('<MainChannel>','<SubChannel>').replace('</MainChannel>','</SubChannel>')
        _,choices=encoder.inspect()
        self.assertEqual(choices,[dict(width=1280,height=720)])
        with self.assertRaises(ValueError): encoder.apply(dict(width=640,height=360,fps=8,kbps=384))

    def test_helper_credentials_never_enter_command_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            encoder=SDKEncoder()
            encoder.binary=Path(tmp)/'helper'
            encoder.binary.write_text('fake')
            response=SimpleNamespace(returncode=0,stdout=json.dumps(dict(ok=True,source='hcnetsdk')),stderr='')
            with patch('backend.sdk_encoder.subprocess.run',return_value=response) as run, patch.dict('os.environ',{'CAMERA_PASSWORD':'private-test-fixture'}):
                encoder.call('inspect')
            self.assertNotIn('private-test-fixture',str(run.call_args.args))
            self.assertEqual(run.call_args.kwargs['env']['CAMERA_PASSWORD'],'private-test-fixture')

    def test_sdk_failure_and_rollback_status_reach_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            encoder=SDKEncoder(); encoder.binary=Path(tmp)/'helper';encoder.binary.write_text('fake')
            response=SimpleNamespace(returncode=1,stdout=json.dumps(dict(ok=False,stage='set_or_readback',sdk_error=23,rollback_verified=False)),stderr='')
            with patch('backend.sdk_encoder.subprocess.run',return_value=response):
                with self.assertRaises(SDKError) as caught: encoder.call('set',86,9,384)
            self.assertFalse(caught.exception.rollback_verified)
            self.assertEqual(caught.exception.code,23)

    def test_source_module_never_silently_falls_back_to_http(self):
        from backend.video_quality import CameraEncoder
        self.assertIs(CameraEncoder,SDKEncoder)


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.encoder=EncoderTests().make_encoder()
        self.manager=VideoQuality(self.encoder,Path(self.tmp.name)/'policy.json')
        self.manager.configure({'profile':'high','auto':True})
        await self.manager.once()
        self.assertFalse(self.manager.error)

    async def test_bad_samples_downshift_then_slow_recovery_with_hysteresis(self):
        with patch('backend.video_quality.time.monotonic',return_value=self.manager.changed_at+25) as clock:
            for _ in range(3):
                self.manager.report(dict(active=True,loss=.1,rtt=.6,stall=False))
                clock.return_value+=2
            self.assertEqual(self.manager.pending['adaptive_level'],1)
            await self.manager.once()
            self.assertEqual(self.manager.current,dict(width=1280,height=720,fps=15,kbps=1024))
            # Short healthy bursts must not immediately raise bandwidth.
            for _ in range(8):
                clock.return_value+=2
                self.manager.report(dict(active=True,loss=0,rtt=.05,stall=False))
            self.assertIsNone(self.manager.pending)
            clock.return_value+=65
            for _ in range(15):
                self.manager.report(dict(active=True,loss=0,rtt=.05,stall=False))
                clock.return_value+=2
            self.assertEqual(self.manager.pending['adaptive_level'],2)

    async def test_invalid_hidden_and_rate_limited_feedback_does_not_downgrade(self):
        for sample in (dict(active=False,loss=.5,rtt=1,stall=True),
                       dict(active=True,loss=float('nan'),rtt=1,stall=True),
                       dict(active=True,loss=.5,rtt=-1,stall=True)):
            self.manager.report(sample)
        self.assertEqual(self.manager.bad,0)
        with patch('backend.video_quality.time.monotonic',return_value=self.manager.changed_at+25):
            for _ in range(5): self.manager.report(dict(active=True,loss=.5,rtt=1,stall=True))
        self.assertEqual(self.manager.bad,1)
        self.assertIsNone(self.manager.pending)
        self.manager.reset_feedback()
        self.assertEqual(self.manager.bad,0)

    async def test_saved_limit_is_restored_and_does_not_contain_credentials(self):
        data=self.manager.path.read_text()
        self.assertNotIn('password',data)
        restored=VideoQuality(self.encoder,self.manager.path)
        await restored.start()
        self.assertEqual(restored.policy,{'profile':'high','auto':True})
        await restored.close()

    async def test_corrupt_policy_shape_cannot_break_backend_startup(self):
        self.manager.path.write_text('[]')
        restored=VideoQuality(self.encoder,self.manager.path)
        await restored.start()
        self.assertEqual(restored.policy,{'auto':False,'profile':'balanced'})
        await restored.close()

    async def test_auth_error_stops_retries_until_explicit_refresh(self):
        calls=[]
        def deny():
            calls.append(1)
            raise SDKError(dict(stage='login',sdk_error=1))
        self.encoder.inspect=deny
        self.manager.refresh()
        await self.manager.once()
        self.manager.refresh_requested=True
        await self.manager.once()
        self.assertEqual(len(calls),1)
        self.manager.refresh()
        await self.manager.once()
        self.assertEqual(len(calls),2)

    async def test_camera_late_power_retains_policy_and_retries_without_control_restart(self):
        inspect=self.encoder.inspect
        self.encoder.inspect=lambda: (_ for _ in ()).throw(OSError('offline'))
        self.manager.configure({'profile':'low','auto':True})
        await self.manager.once()
        self.assertIsNotNone(self.manager.pending)
        self.encoder.inspect=inspect
        with patch('backend.video_quality.time.monotonic',return_value=self.manager.retry_at+1):
            await self.manager.once()
        self.assertEqual(self.manager.current['kbps'],384)


if __name__=='__main__':
    unittest.main()
