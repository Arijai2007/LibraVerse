from django.conf import settings
from django.http import HttpResponse
from reportlab.graphics import renderSVG
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing, Rect
from reportlab.lib import colors


def public_link(request, path):
    if settings.PUBLIC_BASE_URL:
        return settings.PUBLIC_BASE_URL + path
    return request.build_absolute_uri(path)


def qr_label(url):
    size = 216
    drawing = Drawing(size, size)
    drawing.add(Rect(0, 0, size, size, fillColor=colors.white, strokeColor=None))
    drawing.add(
        QrCodeWidget(url, barWidth=size, barHeight=size, barBorder=4, barLevel="M")
    )
    return HttpResponse(renderSVG.drawToString(drawing), content_type="image/svg+xml")
