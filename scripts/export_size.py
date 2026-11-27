"""Shared resolution presets for final MP4 exports."""

RESOLUTIONS = {'source': 0, '1080p': 1080, '4k': 2160}


def delivery_size(width, height, resolution):
    """Keep the aspect ratio and use even dimensions for H.264 yuv420p."""
    scale = max(1, RESOLUTIONS[resolution] / min(width, height))
    return tuple(max(2, round(side * scale / 2) * 2) for side in (width, height))
