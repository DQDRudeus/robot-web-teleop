"""Native HCNetSDK encoder helper transport; no HTTP/ISAPI fallback."""
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

FPS = {0:0,5:1,6:2,7:4,8:6,9:8,10:10,11:12,12:16,13:20,14:15,15:18,16:22,17:25,18:30,25:3,26:5,27:7,28:9,31:24}
RES = {6:(320,240),16:(640,480),18:(800,600),19:(1280,720),20:(1280,960),27:(1920,1080),34:(1024,768),64:(3840,2160),68:(2464,1520),70:(2560,1440),86:(640,360),98:(2688,1520)}
RATES = [0,16,32,48,64,80,96,128,160,192,224,256,320,384,448,512,640,768,896,1024,1280,1536,1792,2048,3072,4096,8192,16384]


class SDKError(ValueError):
    def __init__(self, result):
        self.code = result.get('sdk_error', 0)
        self.stage = result.get('stage', 'unknown')
        self.rollback_verified = result.get('rollback_verified', True)
        super().__init__(f'HCNetSDK {self.stage} error={self.code}' + (' rollback unverified' if not self.rollback_verified else ''))


class SDKEncoder:
    def __init__(self):
        self.binary = Path(os.getenv('CAMERA_ENCODER', str(Path(__file__).resolve().parents[1]/'cpp/hik_encoder')))
        self.options = {}
        self.current = None

    def call(self, *args):
        if not self.binary.is_file():
            raise ValueError('HCNetSDK helper missing; run make -C ./cpp hik_encoder on NX')
        try:
            result = subprocess.run([str(self.binary), *map(str,args)], capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=25,
                                    env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1'})
        except subprocess.TimeoutExpired:
            raise ValueError('HCNetSDK request timed out; source state must be read again') from None
        replies = [line for line in result.stdout.splitlines() if line.startswith('{')]
        if not replies:
            raise ValueError('HCNetSDK helper returned no result; check SDK libraries/build')
        reply = json.loads(replies[-1])
        if not reply.get('ok') or result.returncode:
            raise SDKError(reply)
        if reply.get('source') != 'hcnetsdk':
            raise ValueError('Unexpected encoder provider')
        return reply

    def settings(self, result):
        code = result['resolution_code']
        size = self.options.get(code, {}).get('size', RES.get(code))
        fps = FPS.get(result['fps_code'])
        if size is None or fps is None or not result['kbps']:
            raise ValueError('SDK current encoder value is not mapped; do not guess fps/resolution')
        return dict(width=size[0],height=size[1],fps=fps,kbps=result['kbps'])

    def inspect(self):
        result = self.call('inspect')
        self.options = {}
        if result.get('capabilities_xml'):
            root = ET.fromstring(result['capabilities_xml'])
            # Scope to this channel's MAIN stream, never substream/global ranges.
            def child(node,name):
                return next((c for c in node if c.tag.split('}')[-1]==name),None)
            def text(node,name):
                c=child(node,name)
                return c.text.strip() if c is not None and c.text else ''
            for channel in root.iter():
                if channel.tag.split('}')[-1]!='ChannelEntry' or text(channel,'ChannelNumber')!=os.getenv('CAMERA_CHANNEL','1'):
                    continue
                main=child(channel,'MainChannel')
                if main is None: continue
                for entry in main.iter():
                    if entry.tag.split('}')[-1]!='VideoResolutionEntry': continue
                    raw=text(entry,'Index')
                    if not raw.isdigit() or int(raw) not in RES: continue
                    codec=child(entry,'suportEncodeType')
                    if codec is not None and codec.get('opt') and str(result.get('codec')) not in codec.get('opt').split(','): continue
                    frames={int(x) for x in re.findall(r'\d+',text(entry,'VideoFrameRate'))}
                    nframes={int(x) for x in re.findall(r'\d+',text(entry,'VideoFrameRateN'))}
                    if nframes: frames &= nframes
                    rate=child(entry,'VideoBitrate')
                    rates={int(x) for x in re.findall(r'\d+',text(rate,'Range'))} if rate is not None else set()
                    code=int(raw)
                    self.options[code]=dict(size=RES[code],frames=frames,rates=rates)
        self.current=self.settings(result)
        self.options.setdefault(result['resolution_code'],dict(size=(self.current['width'],self.current['height']),frames={result['fps_code']},rates=set()))
        choices=[dict(width=v['size'][0],height=v['size'][1]) for v in self.options.values()]
        return self.current,sorted(choices,key=lambda v:v['width']*v['height'])

    def adapt(self, settings):
        target=dict(settings)
        option=next((v for v in self.options.values() if v['size']==(target['width'],target['height'])),None)
        if not option: raise ValueError('SDK capability missing for automatic profile')
        frames=[FPS[code] for code in option['frames'] if code in FPS and 0<FPS[code]<=target['fps']]
        if not frames: raise ValueError('SDK has no supported frame rate below requested ceiling')
        target['fps']=max(frames)
        if option['rates']:
            rates=[RATES[code] for code in option['rates'] if code<len(RATES) and 64<=RATES[code]<=target['kbps']]
            if not rates: raise ValueError('SDK has no supported bitrate below requested ceiling')
            target['kbps']=max(rates)
        return target

    def apply(self, settings):
        # Inspect in the same short operation chain before every modification.
        self.inspect()
        code=next((k for k,v in self.options.items() if v['size']==(settings['width'],settings['height'])),None)
        fps=next((k for k,v in FPS.items() if v==settings['fps']),None)
        if code is None or fps is None or fps not in self.options[code]['frames']:
            raise ValueError('SDK capabilities reject resolution/frame rate combination')
        rates=self.options[code]['rates']
        if rates and settings['kbps'] not in [RATES[i] for i in rates if i<len(RATES)]:
            raise ValueError('SDK capabilities reject bitrate')
        actual=self.settings(self.call('set',code,fps,settings['kbps']))
        if actual != settings:
            raise ValueError('SDK response does not match requested settings')
        self.current=actual
        return actual
