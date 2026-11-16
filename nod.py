import pycozmo, time
print("pycozmo", pycozmo.__version__)
with pycozmo.connect() as cli:
    cli.set_head_angle(0.3)
    time.sleep(1)
    cli.set_head_angle(-0.2)
    time.sleep(1)
print("nodded")
