"""Minimal DHAV gateway client: drive the bridge and inspect what comes back.

Stands in for a real gateway provider so the bridge can be checked end to end
without one, and so a remote operator can verify connectivity, auth and media
from their own machine before wiring anything up::

    .venv/bin/python -m dhav_bridge.smoke_client \
        --base-url http://127.0.0.1:18910 --token "$DHAV_AUTH_TOKEN" \
        --input /path/to/reply.wav --out /tmp/dhav-smoke

Writes the echoed audio to ``<out>/output.wav``, the decoded frames to
``<out>/frames/*.png`` and a measurement summary to ``<out>/report.json``.

``--idle-lead S`` reproduces a gateway that parks an idle turn between replies
and supersedes it for each answer (CANCEL idle + START real, no gap), which is
the pattern that stresses turn handover. The idle turn's media and events are
counted separately so they cannot be read as the real turn's.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
import time
import wave
from pathlib import Path

import aiohttp
import cv2
import numpy as np

from . import dhav

SAMPLE_RATE = 24000
FRAME_SAMPLES = 960


def load_pcm(path: str) -> bytes:
    """Any audio file -> mono s16le PCM at 24 kHz."""
    import librosa

    audio, _ = librosa.load(path, sr=SAMPLE_RATE, mono=True)
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def save_wav(path: Path, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)


def i420_to_bgr(data: bytes, width: int, height: int) -> np.ndarray:
    # I420 in memory is a (height * 3 / 2) x width byte image; OpenCV wants it
    # exactly that way, and a transposed view is non-contiguous anyway.
    # Note this decoder is limited-range while the bridge emits full-range, so
    # saved PNGs are slightly off-gamut. That is fine for eyeballing motion.
    buffer = np.frombuffer(data, dtype=np.uint8).reshape(height * 3 // 2, width)
    return cv2.cvtColor(buffer, cv2.COLOR_YUV2BGR_I420)


def rms(pcm: bytes) -> float:
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return float(np.sqrt((samples**2).mean())) if len(samples) else 0.0


async def run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    (out / "frames").mkdir(parents=True, exist_ok=True)
    headers = {"authorization": f"Bearer {args.token}"} if args.token else {}
    connector = None
    if args.base_url.startswith("https"):
        # Trust the bridge's CA explicitly; never disable verification, this
        # process also carries the session bearer token.
        context = ssl.create_default_context(cafile=args.ca) if args.ca else ssl.create_default_context()
        connector = aiohttp.TCPConnector(ssl=context)

    started = time.monotonic()
    report: dict = {"base_url": args.base_url, "input": args.input}
    units: list[dict] = []
    events: list[tuple[float, dict]] = []
    audio_out = bytearray()
    media_end: dict | None = None
    first_media_at: float | None = None
    first_unit_at: float | None = None
    send_started_at: float | None = None
    idle_units = 0
    idle_first_media_at: float | None = None
    idle_failed: dict | None = None
    completed = asyncio.Event()
    session_ready = asyncio.Event()
    failed: dict | None = None

    def stamp() -> float:
        return round(time.monotonic() - started, 3)

    async with aiohttp.ClientSession(connector=connector) as client:
        async with client.post(
            args.base_url.rstrip("/") + "/v1/avatar/sessions",
            headers={**headers, "content-type": "application/json"},
            json={"personaId": args.persona, "protocolVersion": 1},
        ) as response:
            body = await response.text()
            report["create_status"] = response.status
            if response.status != 201:
                print(f"session creation failed: HTTP {response.status} {body}")
                return 1
            created = json.loads(body)
        report["video"] = created["video"]
        print(f"session {created['avatarSessionId']} video={created['video']}")

        async with client.ws_connect(created["input"]["url"], headers={"authorization": f"Bearer {created['input']['token']}"}, max_msg_size=0) as input_ws, \
                   client.ws_connect(created["media"]["url"], headers={"authorization": f"Bearer {created['media']['token']}"}, max_msg_size=0) as media_ws:
            real_generation = 1 if args.idle_lead else 0
            turn = {
                "avatarSessionId": created["avatarSessionId"],
                "responseId": args.response_id,
                "generation": real_generation,
            }
            idle_ref = {**turn, "responseId": "dh-idle", "generation": 0}

            async def read_input() -> None:
                nonlocal first_media_at, failed, idle_first_media_at, idle_failed
                async for message in input_ws:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    frame = json.loads(message.data)
                    events.append((stamp(), frame))
                    name = frame.get("event")
                    if name == "session.ready":
                        print(f"+{stamp()}s session.ready")
                        session_ready.set()
                        continue
                    generation = (frame.get("turn") or {}).get("generation")
                    if generation is not None and generation != real_generation:
                        # The idle lead-in turn: its cancel is expected, and its
                        # first_media says nothing about the real turn's latency.
                        print(f"+{stamp()}s [idle] {name}")
                        if name == "turn.first_media":
                            idle_first_media_at = stamp()
                        elif name == "provider.error":
                            idle_failed = frame
                            print(f"    [idle] {frame.get('code')}: {frame.get('message')}")
                        continue
                    if name == "turn.first_media":
                        first_media_at = stamp()
                        print(f"+{first_media_at}s turn.first_media")
                    elif name == "turn.completed":
                        print(f"+{stamp()}s turn.completed lastSequence={frame.get('lastSequence')}")
                        completed.set()
                    elif name == "turn.cancelled":
                        print(f"+{stamp()}s turn.cancelled reason={frame.get('reason')}")
                        completed.set()
                    elif name == "provider.error":
                        failed = frame
                        print(f"+{stamp()}s provider.error {frame.get('code')}: {frame.get('message')}")
                        completed.set()

            async def read_media() -> None:
                nonlocal media_end, first_unit_at, idle_units
                async for message in media_ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        continue
                    if message.type != aiohttp.WSMsgType.BINARY:
                        continue
                    record = dhav.decode_record(message.data)
                    if record["kind"] == dhav.MEDIA_END:
                        if record["header"]["turn"]["generation"] != real_generation:
                            print(f"+{stamp()}s [idle] media_end reason={record['header']['reason']}")
                            continue
                        media_end = record["header"]
                        print(f"+{stamp()}s media_end lastSequence={media_end['lastSequence']} "
                              f"reason={media_end['reason']}")
                        continue
                    header = record["header"]
                    if header["turn"]["generation"] != real_generation:
                        idle_units += 1
                        continue
                    if first_unit_at is None:
                        first_unit_at = stamp()
                    units.append({"at": stamp(), **header, "bytes": len(message.data)})
                    audio_out.extend(record["audio"])
                    if args.save_frames and header["sequence"] % args.every == 0:
                        image = i420_to_bgr(record["video"], header["video"]["width"], header["video"]["height"])
                        cv2.imwrite(str(out / "frames" / f"f_{header['sequence']:05d}.png"), image)

            async def idle_lead() -> None:
                """Park an idle turn, then supersede it -- no gap, no wait."""
                # The renderer dials upstream *after* session.ready (~0.1 s later)
                # and a START that beats it is rejected as unavailable, which
                # would silently turn this scenario into a plain cold start.
                await asyncio.sleep(1.0)
                await input_ws.send_str(dhav.encode_control(
                    op=dhav.OP_START, request_id="req-idle", turn=idle_ref))
                print(f"+{stamp()}s [idle] start, {args.idle_lead:.1f}s of zero blocks")
                zeros = b"\x00" * (args.chunk_samples * 2)
                deadline = time.monotonic() + args.idle_lead
                sequence = offset = 0
                while time.monotonic() < deadline:
                    await input_ws.send_bytes(dhav.encode_input_pcm(
                        turn=idle_ref, sequence=sequence, sample_offset=offset,
                        samples=args.chunk_samples, payload=zeros))
                    sequence += 1
                    offset += args.chunk_samples
                    await asyncio.sleep(args.chunk_samples / SAMPLE_RATE)
                await input_ws.send_str(dhav.encode_control(
                    op=dhav.OP_CANCEL, request_id="req-idle-cancel", turn=idle_ref,
                    reason="superseded"))
                print(f"+{stamp()}s [idle] cancel after {offset / SAMPLE_RATE:.2f}s, "
                      f"{idle_units} units published so far")
                if idle_failed is not None or not idle_units:
                    # Say so loudly: without a live idle turn there is nothing to
                    # supersede and the run measures nothing new.
                    print(f"+{stamp()}s [idle] WARNING: the idle turn never published media "
                          f"({idle_failed or 'no units'}), supersede was NOT exercised")

            async def upload(pcm: bytes) -> int:
                nonlocal send_started_at
                total = len(pcm) // 2
                print(f"+{stamp()}s sending {total / SAMPLE_RATE:.2f}s of reply audio "
                      f"({args.chunk_samples} samples/record, {'realtime' if args.realtime else 'burst'})")
                await input_ws.send_str(dhav.encode_control(op=dhav.OP_START, request_id="req-1", turn=turn))
                send_started = time.monotonic()
                send_started_at = send_started - started
                sequence = 0
                offset = 0
                cancelled = False
                while offset < total:
                    take = min(args.chunk_samples, total - offset)
                    await input_ws.send_bytes(
                        dhav.encode_input_pcm(turn=turn, sequence=sequence, sample_offset=offset,
                                              samples=take, payload=pcm[offset * 2 : (offset + take) * 2])
                    )
                    sequence += 1
                    offset += take
                    if args.realtime:
                        target = send_started + offset / SAMPLE_RATE
                        if target > time.monotonic():
                            await asyncio.sleep(target - time.monotonic())
                    if args.cancel_after and offset / SAMPLE_RATE >= args.cancel_after:
                        print(f"+{stamp()}s cancelling after {args.cancel_after}s of input")
                        await input_ws.send_str(dhav.encode_control(
                            op=dhav.OP_CANCEL, request_id="req-cancel", turn=turn,
                            reason="user_interruption"))
                        cancelled = True
                        break
                if not cancelled:
                    await input_ws.send_str(dhav.encode_control(
                        op=dhav.OP_FINISH, request_id="req-2", turn=turn,
                        last_audio_sequence=sequence - 1, total_samples=total))
                    print(f"+{stamp()}s finish (sent {sequence} records in "
                          f"{time.monotonic() - send_started:.2f}s)")
                return total

            readers = [
                asyncio.create_task(read_input(), name="read-input"),
                asyncio.create_task(read_media(), name="read-media"),
            ]

            def reader_died(task: asyncio.Task) -> None:
                # A crashed reader otherwise looks exactly like "the renderer
                # stopped sending", which sends you debugging the wrong side.
                if task.cancelled() or task.exception() is None:
                    return
                nonlocal failed
                failed = failed or {"code": "client_error", "message": repr(task.exception())}
                print(f"reader {task.get_name()} died: {task.exception()!r}")
                completed.set()

            for task in readers:
                task.add_done_callback(reader_died)
            total_samples = 0
            # Loaded up front on purpose: librosa blocks the event loop for
            # seconds, and doing it between CANCEL and START would stretch the
            # supersede gap well past the flush window it is meant to test.
            pcm = load_pcm(args.input)
            try:
                await asyncio.wait_for(session_ready.wait(), timeout=30)
            except asyncio.TimeoutError:
                failed = {"code": "no_ready", "message": "session.ready never arrived"}
                completed.set()
            if not failed:
                if args.idle_lead:
                    await idle_lead()
                total_samples = await upload(pcm)

            try:
                await asyncio.wait_for(completed.wait(), timeout=args.timeout)
            except asyncio.TimeoutError:
                print(f"timed out after {args.timeout}s waiting for turn.completed")
                report["status"] = "timeout"

            if not args.keep_open:
                await input_ws.send_str(dhav.encode_control(op=dhav.OP_CLOSE, request_id="req-close"))
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

        async with client.delete(
            f"{args.base_url.rstrip('/')}/v1/avatar/sessions/{created['avatarSessionId']}", headers=headers
        ) as response:
            report["delete_status"] = response.status

    save_wav(out / "output.wav", bytes(audio_out))

    sequences = [u["sequence"] for u in units]
    pts = [u["ptsUs"] for u in units]
    report.update({
        "status": report.get("status") or ("failed" if failed else "ok"),
        "provider_error": failed,
        "units": len(units),
        "sequence_continuous": sequences == list(range(len(sequences))),
        "pts_monotonic": all(b > a for a, b in zip(pts, pts[1:])),
        "first_media_at_s": first_media_at,
        "first_unit_at_s": first_unit_at,
        # first_media_at_s is measured from process start and so includes this
        # client's own librosa load of the input file (seconds of it). This is
        # the number a gateway actually experiences: first record -> first unit.
        "first_media_after_send_s": (
            round(first_media_at - send_started_at, 3)
            if first_media_at is not None and send_started_at is not None else None
        ),
        "audio_seconds": round(len(audio_out) / 2 / SAMPLE_RATE, 3),
        "input_seconds": round(total_samples / SAMPLE_RATE, 3),
        "idle_lead_s": args.idle_lead or None,
        "idle_units_published": idle_units if args.idle_lead else None,
        "idle_first_media_at_s": idle_first_media_at,
        "idle_lead_exercised": (idle_failed is None and idle_units > 0) if args.idle_lead else None,
        "idle_error": idle_failed,
        "media_end": media_end,
        "output_rms": round(rms(bytes(audio_out)), 4),
        "unit_bytes_median": int(np.median([u["bytes"] for u in units])) if units else 0,
        "wall_seconds": round(time.monotonic() - started, 2),
        "events": [{"at": at, "event": e.get("event"), "code": e.get("code")} for at, e in events
                   if e.get("event") != "heartbeat"],
    })
    if len(units) >= 2:
        span = units[-1]["at"] - units[0]["at"]
        report["publish_rate_hz"] = round((len(units) - 1) / span, 2) if span > 0 else None
        # The gateway publishes on an audio master clock, so a rate below the
        # frame rate means the browser will starve. The gap tells a uniformly
        # slow renderer apart from an occasional stall.
        report["max_unit_gap_s"] = round(max(b["at"] - a["at"] for a, b in zip(units, units[1:])), 3)
        report["realtime_factor"] = round(report["audio_seconds"] / span, 3) if span > 0 else None
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print("\n" + "=" * 62)
    summary_keys = ["status", "units", "sequence_continuous", "pts_monotonic", "first_media_at_s",
                    "first_media_after_send_s", "first_unit_at_s", "audio_seconds", "input_seconds",
                    "output_rms", "unit_bytes_median", "publish_rate_hz", "max_unit_gap_s",
                    "realtime_factor", "wall_seconds"]
    if args.idle_lead:
        summary_keys += ["idle_lead_s", "idle_lead_exercised", "idle_units_published",
                         "idle_first_media_at_s"]
    for key in summary_keys:
        print(f"  {key:<22} {report.get(key)}")
    print(f"  {'media_end':<22} {media_end}")
    if failed:
        print(f"  {'provider_error':<22} {failed.get('code')}: {failed.get('message')}")
    print(f"  output -> {out}/output.wav, {len(list((out / 'frames').glob('*.png')))} frames, report.json")
    print("=" * 62)
    return 0 if report["status"] == "ok" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:18910")
    parser.add_argument("--token", default=None, help="bearer token for session create/delete")
    parser.add_argument("--persona", default="flashhead-default")
    parser.add_argument("--response-id", default="resp-smoke-1")
    parser.add_argument("--input", required=True, help="reply audio to render (any format librosa reads)")
    parser.add_argument("--out", default="/tmp/dhav-smoke")
    parser.add_argument("--chunk-samples", type=int, default=1920, help="samples per INPUT_PCM record")
    parser.add_argument("--realtime", action="store_true", help="pace the upload instead of bursting it")
    parser.add_argument("--cancel-after", type=float, default=0, help="cancel the turn after N seconds of input")
    parser.add_argument("--idle-lead", type=float, default=0,
                        help="run an all-zero idle turn for N seconds and supersede it "
                             "(CANCEL idle + START real) before streaming --input")
    parser.add_argument("--timeout", type=float, default=120, help="seconds to wait for turn.completed")
    parser.add_argument("--save-frames", action="store_true", default=True)
    parser.add_argument("--every", type=int, default=25, help="save every Nth frame as PNG")
    parser.add_argument("--keep-open", action="store_true", help="do not send close (leave the session up)")
    parser.add_argument("--ca", default=None, help="CA file for an https/wss base URL")
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
