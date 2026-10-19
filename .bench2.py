import av, time, statistics, numpy as np
import av.codec.hwaccel as h

raw=open('.tmp/live.h264','rb').read()
pkts=[bytes(p) for p in av.CodecContext.create('h264','r').parse(raw)]
print(f"{len(pkts)} packets, {len(raw)/1e6:.2f} MB\n")

def convert(frame):
    """Exact replica of VideoDecoder._convert (reformat + 3 plane copies)."""
    if frame.width<=0 or frame.height<=0: return None
    ref=frame.reformat(format='yuv420p')
    planes=[]
    for p in ref.planes:
        line,h_,w=p.line_size,p.height,p.width
        if line<w or h_<=0 or w<=0: return None
        buf=np.frombuffer(p,dtype=np.uint8,count=line*h_)
        planes.append(buf.reshape(h_,line)[:,:w].copy())
    return planes

def run(label, hw=None):
    ctx=av.CodecContext.create('h264','r', hwaccel=hw) if hw else av.CodecContext.create('h264','r')
    if not hw:
        try: ctx.thread_type='SLICE'
        except Exception: pass
    dec=[]; conv=[]; n=0
    t0=time.perf_counter()
    for b in pkts:
        t1=time.perf_counter(); frames=ctx.decode(av.Packet(b)); dec.append((time.perf_counter()-t1)*1000)
        for f in frames:
            t2=time.perf_counter(); convert(f); conv.append((time.perf_counter()-t2)*1000); n+=1
    total=(time.perf_counter()-t0)*1000
    fmt=frames[-1].format.name if frames else '?'
    d=sorted(dec); c=sorted(conv)
    print(f"{label:18} decode_med={d[len(d)//2]:5.2f}ms  conv_med={c[len(c)//2] if c else 0:5.2f}ms  "
          f"total/frame={total/max(n,1):5.2f}ms  fmt={fmt}")
    return total/max(n,1)

base=run("software")
for dt in ('qsv','amf','dxva2','d3d11va'):
    try:
        v=run(f"hw:{dt}", hw=h.HWAccel(device_type=dt))
        print(f"{'':18} -> {'FASTER' if v<base else 'SLOWER'} than software ({100*(v-base)/base:+.0f}%)")
    except Exception as e:
        print(f"hw:{dt:14} FAILED {type(e).__name__}: {str(e)[:70]}")
