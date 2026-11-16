"""Offline tests: framing, CRC and the head-angle maths against a simulated earbud.

Run with:  python tests/test_budstrack.py
"""
import math
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import budstrack as b

X, Y, Z = (1, 0, 0), (0, 1, 0), (0, 0, 1)


def aq(axis, deg):
    a = math.radians(deg) / 2
    s = math.sin(a)
    return (axis[0] * s, axis[1] * s, axis[2] * s, math.cos(a))


def test_protocol():
    # example frame body from GalaxyBudsClient's Crc16.cs; CRC bytes on the wire are 0F F3
    body = bytes([0x61, 0x02, 0x00, 0x4B, 0x5F, 0x01, 0x00, 0x00, 0x00, 0x01, 0x05, 0x00, 0x02, 0x00, 0x13])
    assert struct.pack("<H", b.crc16(body)) == bytes([0x0F, 0xF3])
    m = b.encode_msg(195, b"\x00")
    buf = bytearray(b"\x00\x11" + m + m[:3])  # junk, one frame, a partial frame
    assert list(b.decode_stream(buf)) == [(195, b"\x00")] and bytes(buf) == m[:3]
    bad = bytearray(m)
    bad[4] ^= 0xFF  # corrupt payload -> dropped, following frame still decoded
    assert list(b.decode_stream(bytearray(bytes(bad) + m))) == [(195, b"\x00")]


class FakeBud:
    """Earbud with an arbitrary tilt in the ear; world frame has X up and an arbitrary heading."""
    HEAD_TO_WORLD = (0.5, 0.5, 0.5, 0.5)  # head X->world Y, Y->Z, Z(up)->X(up)

    def __init__(self, mount, heading_deg):
        self.mount, self.heading = b.q_norm(mount), aq(X, heading_deg)

    def sample(self, head_rot):
        earbud_to_world = b.q_mul(b.q_mul(b.q_mul(self.heading, self.HEAD_TO_WORLD), head_rot), self.mount)
        return b.q_conj(earbud_to_world)  # the Buds report world -> earbud


POSES = [
    ("right 30", aq(Z, -30), (30, 0, 0)),
    ("left 50", aq(Z, 50), (-50, 0, 0)),
    ("up 20", aq(X, 20), (0, 20, 0)),
    ("down 15", aq(X, -15), (0, -15, 0)),
    ("tilt right 15", aq(Y, 15), (0, 0, 15)),
    ("right 40 + up 10", b.q_mul(aq(Z, -40), aq(X, 10)), (40, 10, 0)),
    ("right 70 + down 20 + tilt 5", b.q_mul(b.q_mul(aq(Z, -70), aq(X, -20)), aq(Y, 5)), (70, -20, 5)),
]


def check(orient, bud, label):
    for name, rot, expected in POSES:
        got = orient.update(bud.sample(rot))
        assert all(abs(g - e) < 0.01 for g, e in zip(got, expected)), (label, name, got)


def test_angles():
    for mount in [(0.3, -0.5, 0.2, 0.78), (-0.6, 0.1, 0.7, 0.3), (0, 0, 0, 1)]:
        cfg = dict(b.DEFAULTS, sensor_smoothing=0, drift_fix=False)
        orient = b.Orientation(cfg)
        bud = FakeBud(mount, heading_deg=73)
        orient.update(bud.sample(aq(Z, 0)))
        orient.recenter()
        orient.update(bud.sample(aq(X, 25)))  # calibration gesture: head lifted
        res = b.solve_calibration(orient.relative_rotvec())
        assert isinstance(res, list), res
        cfg["pitch_axis"] = res
        orient.update(bud.sample(aq(Z, 0)))
        orient.recenter()
        check(orient, bud, "calibrated")

        # The sensor's heading reference is arbitrary and drifts: a new session with a
        # different heading must work WITHOUT recalibrating.
        for heading in (-140, 10, 200):
            bud2 = FakeBud(mount, heading)
            orient.reset()
            orient.update(bud2.sample(aq(Z, 0)))
            check(orient, bud2, "heading %d" % heading)

        # Recentring while looking 20 degrees to the side keeps pitch on the head's own axis.
        orient.reset()
        orient.update(bud.sample(aq(Z, -20)))
        got = orient.update(bud.sample(b.q_mul(aq(Z, -20), aq(X, 15))))
        assert all(abs(g - e) < 0.01 for g, e in zip(got, (0, 15, 0))), got


def _body(bud, cfg, rot):
    q = bud.sample(rot)
    return b.q_conj(q) if cfg["q_world_to_body"] else q


def _hold(guide, q, t, n=18, dt=0.05):
    info = None
    for i in range(n):
        info = guide.push(q, t + i * dt)
    return info, t + n * dt


def test_guided_calibration():
    for mount in [(0.3, -0.5, 0.2, 0.78), (-0.6, 0.1, 0.7, 0.3), (0, 0, 0, 1)]:
        cfg = dict(b.DEFAULTS, sensor_smoothing=0, drift_fix=False, calibrated=False)
        bud = FakeBud(mount, heading_deg=40)
        guide = b.HeadGuide(cfg["world_up"])
        t = 0.0
        _hold(guide, _body(bud, cfg, aq(Z, 0)), t)
        assert guide.phase == "nod", guide.info
        t = 1.0
        _hold(guide, _body(bud, cfg, aq(X, 20)), t)
        assert guide.nod is not None and guide.phase == "tilt", guide.info
        t = 2.2
        _hold(guide, _body(bud, cfg, aq(Z, 0)), t)
        assert guide.armed, guide.info
        t = 3.4
        _hold(guide, _body(bud, cfg, aq(Y, 18)), t)
        assert guide.tilt is not None and guide.phase == "turn", guide.info
        t = 4.6
        _hold(guide, _body(bud, cfg, aq(Z, 0)), t)
        t = 5.8
        info, _t = _hold(guide, _body(bud, cfg, aq(Z, -25)), t)
        assert guide.phase == "done" and info["save"], info
        cfg.update(guide.saved)
        assert cfg["invert_pitch"] is False and cfg["invert_roll"] is False
        assert cfg["invert_yaw"] is False
        orient = b.Orientation(cfg)
        orient.update(bud.sample(aq(Z, 0)))
        orient.recenter()
        check(orient, bud, "guided %s" % (mount,))

        # A head turn is not accepted as a nod.
        bad = b.HeadGuide(cfg["world_up"])
        _hold(bad, _body(bud, cfg, aq(Z, 0)), 0.0)
        info, _t = _hold(bad, _body(bud, cfg, aq(Z, -30)), 1.0)
        assert bad.nod is None and info["text"] == "cal_nod_wrong"


def test_calibration_rejects_bad_gestures():
    cfg = dict(b.DEFAULTS, sensor_smoothing=0, drift_fix=False)
    orient = b.Orientation(cfg)
    bud = FakeBud((0.3, -0.5, 0.2, 0.78), 0)
    orient.update(bud.sample(aq(Z, 0)))
    orient.recenter()
    orient.update(bud.sample(aq(X, 4)))
    assert b.solve_calibration(orient.relative_rotvec()) == ("cal_small",)
    orient.update(bud.sample(aq(Z, -40)))  # a head turn is not a head lift
    assert b.solve_calibration(orient.relative_rotvec())[0] == "cal_axis"


def test_direct_output_format():
    # the shared block must match opentrack's FTHeap byte for byte
    assert b.ctypes.sizeof(b.FTHeap) == 108
    assert b.FTHeap.GameID.offset == 92 and b.FTHeap.Table.offset == 96 and b.FTHeap.GameID2.offset == 104
    # key table: same field order as opentrack's csv.cpp; "V160" entries are unencrypted
    assert b.parse_game_table("00721B4BFF8B3EA3296600", "V160") == bytes(8)
    assert b.parse_game_table("0102030405060708090A0B", "V170") == bytes([6, 5, 4, 3, 10, 9, 8, 7])
    assert b.parse_game_table("short", "V170") == bytes(8)


def test_pose_filter():
    import random
    random.seed(7)
    cfg = dict(b.DEFAULTS)
    dt = 0.008

    # a resting head with sensor noise must give a (nearly) motionless output
    f = b.PoseFilter(cfg)
    outs = [f.step((10 + random.uniform(-0.4, 0.4), random.uniform(-0.4, 0.4), 0.0), dt) for _ in range(1500)]
    tail = [o[0] for o in outs[500:]]
    assert max(tail) - min(tail) < 0.12, max(tail) - min(tail)

    # a real head turn must still arrive quickly and without overshoot
    f = b.PoseFilter(cfg)
    f.step((0.0, 0.0, 0.0), dt)
    trace = [f.step((30.0, 0.0, 0.0), dt)[0] for _ in range(125)]
    assert trace[30] > 27.0, trace[30]          # 90 % within a quarter of a second
    assert max(trace) <= 30.0 and abs(trace[-1] - 30.0) < 0.5

    # yaw wraps at +-180 instead of spinning the long way round
    f = b.PoseFilter(cfg)
    f.step((178.0, 0.0, 0.0), dt)
    y = [f.step((-178.0, 0.0, 0.0), dt)[0] for _ in range(60)]
    assert all(abs(v) > 170 for v in y), y[:5]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
