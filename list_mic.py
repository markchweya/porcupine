from pvrecorder import PvRecorder

for i, d in enumerate(PvRecorder.get_available_devices()):
    print(i, d)
