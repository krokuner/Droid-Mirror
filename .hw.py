import av, time, statistics, sys
import av.codec.hwaccel as h

data = open('.tmp/bench.h264','rb').read()
pkts = [bytes(p) for p in av.CodecContext.create('h264','r').parse(data)]
print(f"{len(pkts)} packets, {len(data)} bytes")

def run(label, hw=None):
    ctx = av.CodecContext.create('h264','r', hwaccel=hw) if hw else av.CodecContext.create('h264','r')
    if not hw:
        try: ctx.thread_type='SLICE'
        except Exception: pass
    out=[]; times=[]
    for b in pkts:
        t0=time.perf_counter()
        fr = ctx.decode(av.Packet(b))
        times.append((time.perf_counter()-t0)*1000)
        for f in fr: out.append(f)
    # convert cost
    t0=time.perf_counter()
    conv=[f.to_ndarray(format='yuv420p') for f in out[-3:]]
    conv_ms=(time.perf_counter()-t0)*1000/max(len(conv),1)
    fmt = out[-1].format.name if out else 'none'
    print(f"{label:26} frames={len(out):3} raw_decode_med={statistics.median(times):5.2f}ms "
          f"to_yuv420p={conv_ms:5.2f}ms/frame lastfmt={fmt}")
    return out

run("software")
for dt in ('d3d11va','dxva2','qsv','amf','cuda','d3d12va'):
    try:
        run(f"hw:{dt}", hw=h.HWAccel(device_type=dt))
    except Exception as e:
        print(f"hw:{dt:20} FAILED {type(e).__name__}: {str(e)[:90]}")
