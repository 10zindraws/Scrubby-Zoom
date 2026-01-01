from krita import Krita
from .scrubby_zoom import ScrubbyZoomExtension

Krita.instance().addExtension(ScrubbyZoomExtension(Krita.instance()))
