import pycozmo, time
count = [0]
def on_frame(cli, image):
    count[0] += 1
    if count[0] in (10, 30):
        image.save(f"peek{count[0]}.jpg")
        print("saved frame", count[0])
with pycozmo.connect() as cli:
    cli.set_head_angle(0.1)
    cli.enable_camera(enable=True, color=False)
    cli.add_handler(pycozmo.event.EvtNewRawCameraImage, on_frame)
    time.sleep(5)
    print("camera_config:", getattr(cli, "camera_config", None))
