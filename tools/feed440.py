import os, sys, time

HOME  = os.environ.get("BRIDGE_HOME", "/home/arduino")
BLOCK = 2048
data = open(HOME + "/test440.raw", "rb").read()
f = os.open(HOME + "/audio.fifo", os.O_WRONLY)
t0 = time.perf_counter(); i = n = 0
while i < len(data):
    os.write(f, data[i:i+BLOCK]); i += BLOCK; n += 1
    d = t0 + n * BLOCK / 176400.0 - time.perf_counter()
    if d > 0: time.sleep(d)
os.close(f)
