"""What the model is told an image is: its id and a label, never its pixels.

The label follows the route (RAI-318). By the time a helper runs, the router
has already decided what a shop's camera photo is (RAI-304): a list photo goes
to Order, a photo for a pending credit or stock ask goes to that helper, a
photo with payment words goes to Proof. Calling every camera photo a "shelf
photo" made the Order helper ask shops to type a list it could read.
"""

from __future__ import annotations

from mimi.harness.inbound import Inbound

# Routed skill -> what a camera photo is to that helper. Anything else is "photo".
CAMERA_LABELS: dict[str, str] = {
    "order/order-intake": "order list photo",
    "credit/credit-request": "shelf photo",
    "stock/stock-count": "shelf photo",
}


def media_pointer(inbound: Inbound, skill: str = "") -> str:
    """``[<label> ph_<sha256[:12]>]`` for an image, "" for anything else."""
    m = inbound.payload.media
    if m.kind != "image" or not m.sha256:
        return ""
    from mimi.harness.router import media_shape

    if m.camera_exif:
        what = CAMERA_LABELS.get(skill, "photo")
    else:
        what = {"screenshot": "payment screenshot"}.get(media_shape(m), "image")
    return f"[{what} ph_{m.sha256[:12]}]"
