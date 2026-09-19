import json
import subprocess


def command(args, timeout=500):
    # No shell, no network protocols, bounded execution; stderr can contain private filenames.
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError("Media processing failed")
    return result.stdout


def probe(path):
    data = command(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        30,
    )
    return json.loads(data)


def mock_clip(path, duration):
    command(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x292b2f:s=360x640:r=24",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ]
    )


def assemble_files(paths, durations, directory):
    normalized = []
    for index, (path, duration) in enumerate(zip(paths, durations)):
        meta = probe(path)
        has_audio = any(s["codec_type"] == "audio" for s in meta["streams"])
        if not any(s["codec_type"] == "video" for s in meta["streams"]):
            raise ValueError("Missing video stream")
        out = directory / f"normalized-{index}.mp4"
        args = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(path),
        ]
        if not has_audio:
            args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        args += [
            "-map",
            "0:v:0",
            "-map",
            "0:a:0" if has_audio else "1:a:0",
            "-vf",
            "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,fps=24,tpad=stop_mode=clone:stop_duration=30",
            "-af",
            "aresample=48000,apad",
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "2",
            str(out),
        ]
        command(args)
        normalized.append(out)
    manifest = directory / "concat.txt"
    manifest.write_text("".join(f"file '{p.name}'\n" for p in normalized))
    result = directory / "video.mp4"
    command(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            str(manifest),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(result),
        ]
    )
    return result
