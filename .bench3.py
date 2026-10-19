import av, time, statistics, numpy as np
import av.codec.hwaccel as h
raw=open('.tmp/live.h264','rb').read()
pkts=[bytes(p) for p in av.CodecContext.create('h264','r').parse(raw)]

def convert(frame):
    ref=frame.reformat(format='yuv420p'); out=[]
    for p in ref.planes:
        line,hh,ww=p.line_size,p.height,p.width
        if line<ww: return None
        b=np.frombuffer(p,dtype=np.uint8,count=line*hh)
        out.append(b.reshape(hh,line)[:,:ww].copy())
    return out

def run(hw=None):
    ctx=av.CodecContext.create('h264','r', hwaccel=hw) if hw else av.CodecContext.create('h264','r')
    if not hw:
        try: ctx.thread_type='SLICE'
        except Exception: pass
    t0=time.perf_counter(); n=0
    for b in pkts:
        for f in ctx.decode(av.Packet(b)):
            convert(f); n+=1
    return (time.perf_counter()-t0)*1000/max(n,1)

cands={'software':None}
for dt in ('qsv','amf','dxva2','d3d11va'):
    try:
        av.CodecContext.create('h264','r', hwaccel=h.HWAccel(device_type=dt)); cands[f'hw:{dt}']=dt
    except Exception: pass

res={}
for name,dt in cands.items():
    runs=[]
    for _ in range(4):
        try: runs.append(run(h.HWAccel(device_type=dt) if dt else None))
        except Exception as e: print(name,"fail",e); break
    if runs: res[name]=statistics.median(runs)
base=res.get('software',0)
print()
for name,v in sorted(res.items(), key=lambda x:x[1]):
    tag='' if 'software' not in name else '  <- baseline'
    print(f"{name:16} median of 4 runs = {v:6.2f} ms/frame  ({100*(v-base)/base:+5.1f}%){tag}")
