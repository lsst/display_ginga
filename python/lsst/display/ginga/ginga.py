#
# LSST Data Management System
# Copyright 2008, 2009, 2010, 2015 LSST Corporation.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <http://www.lsstcorp.org/LegalNotices/>.
#

"""
An afw.display backend that uses a Ginga viewer embedded in a Jupyter widget.

Quick Start Instructions
------------------------

Install, via e.g. conda or pip:

 - ipywidgets
 - ipyevents
 - ginga

Register the Jupyter extensions with::

    jupyter nbextension enable --py --sys-prefix widgetsnbextension
    jupyter nbextension enable --py --sys-prefix ipyevents

Optionally install from source:

 - aggdraw, forked at https://github.com/ejeschke/aggdraw

Launch a Jupyter notebook, start a kernel, and do::

    import lsst.afw.display
    lsst.afw.display.setDefaultBackend("ginga")
    display = lsst.afw.display.Display(dims=(800, 600)) # size in screen pixels
    display.embed()

Make sure the last call is the last line in the cell whose output should
contain the viewer (its return value needs to be what Jupyter sees as the
"result" of the cell).

You can then use regular afw.display commands (in other cells) to show images
or other objects in that cell, and ginga's usual keyboard commands to pan,
zoom, scale, etc. (see http://ginga.readthedocs.io/en/latest/quickref.html).

Note that to save the state of the image display widget for static rendering
(e.g. in GitHub), the ipywidgets package adds a new menu to Jupyter that
includes a "Save Widget State" option.

Known Issues
------------

 - Drawing a lot of ellipses (e.g. using Display.dot on all objects in
   a full-patch or full-sensor catalog) can bring things to a standstill - it's
   not only slow to draw them, but the responsivity of the viewer tanks when
   there are a lot of overlaid objects.  This can be improved somewhat by using
   circles instead of ellipses or installing aggdraw.

 - Only the image itself is currently displayed, and hence interaction is
   limited to mouse and keyboard commands.  This also means we can't even test
   whether WCS information is being properly propagated, because we don't have
   a way to see where the cursor is believed to be.

 - Events and callbacks are not yet supported.

"""

from __future__ import absolute_import, division, print_function

import math
import sys

import ipywidgets
import lsst.afw.display.ds9Regions as ds9Regions
import lsst.afw.display.interface as interface
import lsst.afw.display.virtualDevice as virtualDevice
import lsst.afw.geom as afwGeom
from ginga.misc.log import get_logger
from ginga.web.jupyterw.ImageViewJpw import EnhancedCanvasView


def gingaVersion():
    """Return the version of ginga in use, as a string"""
    from ginga.version import version

    return version


class GingaEvent(interface.Event):
    """An event generated by a mouse or key click on the display"""

    def __init__(self, k, x, y):
        interface.Event.__init__(self, k, x, y)


class DisplayImpl(virtualDevice.DisplayImpl):
    def __init__(self, display, verbose=False, dims=None, canvas_format="jpeg", *args, **kwargs):
        """
        Initialise a ginga display

        canvas_type file type for displays ('jpeg': fast; 'png' : better, slow)
        dims        (x,y) dimensions of image display widget in screen pixels
        """
        virtualDevice.DisplayImpl.__init__(self, display, verbose=False)
        if dims is None:
            # TODO: get defaults from Jupyter defaults?
            width, height = 1024, 768
        else:
            width, height = dims
        self._imageWidget = ipywidgets.Image(format=canvas_format, width=width, height=height)
        logger = get_logger("ginga", log_stderr=True, level=40)
        self._viewer = EnhancedCanvasView(logger=logger)
        self._viewer.set_widget(self._imageWidget)
        bd = self._viewer.get_bindings()
        bd.enable_all(True)
        self._canvas = self._viewer.add_canvas()
        self._canvas.enable_draw(False)
        self._maskTransparency = 0.8
        self._redraw = True

    def embed(self):
        """Attach this display to the output of the current cell."""
        return self._viewer.embed()

    #
    # Extensions to the API
    #
    def get_viewer(self):
        """Return the ginga viewer"""
        return self._viewer

    def show_color_bar(self, show=True):
        """Show (or hide) the colour bar"""
        self._viewer.show_color_bar(show)

    def show_pan_mark(self, show=True, color="red"):
        """Show (or hide) the colour bar"""
        self._viewer.show_pan_mark(show, color)

    def _setMaskTransparency(self, transparency, maskplane):
        """Specify mask transparency (percent); or None to not set it when
        loading masks"""
        if maskplane is not None:
            print(
                "display_ginga is not yet able to set transparency for individual maskplanes" % maskplane,
                file=sys.stderr,
            )
            return

        self._maskTransparency = 0.01 * transparency

    def _getMaskTransparency(self, maskplane=None):
        """Return the current mask transparency"""
        return self._maskTransparency

    def _mtv(self, image, mask=None, wcs=None, title=""):
        """Display an Image and/or Mask on a ginga display"""
        self._erase()
        self._canvas.delete_all_objects()
        if image:
            # We'd call
            #   self._viewer.load_data(image.getArray())
            # except that we want to include the wcs
            #
            # Still need to handle the title
            #
            from ginga import AstroImage

            astroImage = AstroImage.AstroImage(logger=self._viewer.logger, data_np=image.getArray())
            if wcs is not None:
                astroImage.set_wcs(WcsAdaptorForGinga(wcs))

            self._viewer.set_image(astroImage)

        if mask:
            import numpy as np
            from ginga.RGBImage import RGBImage  # 8 bpp RGB[A] images
            from matplotlib.colors import colorConverter

            # create a 3-channel RGB image + alpha
            maskRGB = np.zeros((mask.getHeight(), mask.getWidth(), 4), dtype=np.uint8)
            maska = mask.getArray()
            nSet = np.zeros_like(maska, dtype="uint8")

            R, G, B, A = 0, 1, 2, 3  # names for colours and alpha plane
            colorGenerator = self.display.maskColorGenerator(omitBW=True)

            for maskPlaneName, maskPlaneNum in mask.getMaskPlaneDict().items():
                isSet = maska & (1 << maskPlaneNum) != 0
                if (isSet == 0).all():  # no bits set; nowt to do
                    continue

                color = self.display.getMaskPlaneColor(maskPlaneName)

                if not color:  # none was specified
                    color = next(colorGenerator)
                elif color.lower() == "ignore":
                    continue

                r, g, b = colorConverter.to_rgb(color)
                maskRGB[:, :, R][isSet] = 255 * r
                maskRGB[:, :, G][isSet] = 255 * g
                maskRGB[:, :, B][isSet] = 255 * b

                nSet[isSet] += 1

            alpha = self.display.getMaskTransparency()  # Bug!  Fails to return a value
            if alpha is None:
                alpha = self._getMaskTransparency()

            maskRGB[:, :, A] = 255 * (1 - alpha)
            maskRGB[:, :, A][nSet == 0] = 0

            nSet[nSet == 0] = 1  # avoid division by 0
            for C in (R, G, B):
                maskRGB[:, :, C] //= nSet

            rgb_img = RGBImage(data_np=maskRGB)

            Image = self._canvas.get_draw_class("image")  # the appropriate class
            maskImageRGBA = Image(0, 0, rgb_img)

            self._canvas.add(maskImageRGBA)

    #
    # Graphics commands
    #

    def _buffer(self, enable=True):
        self._redraw = not enable

    def _flush(self):
        self._viewer.redraw(whence=3)

    def _erase(self):
        """Erase the display"""
        self._canvas.delete_all_objects()

    def _dot(self, symb, c, r, size, ctype, fontFamily="helvetica", textAngle=None):
        """Draw a symbol at (col,row) = (c,r) [0-based coordinates]

        Possible values are:
            +                Draw a +
            x                Draw an x
            *                Draw a *
            o                Draw a circle
            @:Mxx,Mxy,Myy    Draw an ellipse with moments (Mxx, Mxy, Myy)
                             (argument size is ignored)
            An object derived from afwGeom.ellipses.BaseCore Draw the ellipse
                             (argument size is ignored)

            Any other value is interpreted as a string to be drawn. Strings
            obey the fontFamily (which may be extended with other
            characteristics, e.g. "times bold italic".  Text will be drawn
            rotated by textAngle (textAngle is ignored otherwise).

        N.b. objects derived from BaseCore include Axes and Quadrupole.
        """
        if isinstance(symb, afwGeom.ellipses.BaseCore):
            Ellipse = self._canvas.get_draw_class("ellipse")

            self._canvas.add(
                Ellipse(
                    c,
                    r,
                    xradius=symb.getA(),
                    yradius=symb.getB(),
                    rot_deg=math.degrees(symb.getTheta()),
                    color=ctype,
                ),
                redraw=self._redraw,
            )
        elif symb == "o":
            Circle = self._canvas.get_draw_class("circle")
            self._canvas.add(Circle(c, r, radius=size, color=ctype), redraw=self._redraw)
        else:
            Line = self._canvas.get_draw_class("line")
            Text = self._canvas.get_draw_class("text")

            for ds9Cmd in ds9Regions.dot(symb, c, r, size, fontFamily="helvetica", textAngle=None):
                tmp = ds9Cmd.split("#")
                cmd = tmp.pop(0).split()
                comment = tmp.pop(0) if tmp else ""  # noqa: F841

                cmd, args = cmd[0], cmd[1:]
                if cmd == "line":
                    self._canvas.add(Line(*[float(p) - 1 for p in args], color=ctype), redraw=self._redraw)
                elif cmd == "text":
                    x, y = [float(p) - 1 for p in args[0:2]]
                    self._canvas.add(Text(x, y, symb, color=ctype), redraw=self._redraw)
                else:
                    raise RuntimeError(ds9Cmd)

    def _drawLines(self, points, ctype):
        """Connect the points, a list of (col,row)
        Ctype is the name of a colour (e.g. 'red')
        """
        Line = self._canvas.get_draw_class("line")
        p0 = points[0]
        for p in points[1:]:
            self._canvas.add(Line(p0[0], p0[1], p[0], p[1], color=ctype), redraw=self._redraw)
            p0 = p

    #
    # Set gray scale
    #
    def _scale(self, algorithm, min, max, unit, *args, **kwargs):
        self._viewer.set_color_map("gray")
        self._viewer.set_color_algorithm(algorithm)

        if min == "zscale":
            self._viewer.set_autocut_params("zscale", contrast=0.25)
            self._viewer.auto_levels()
        elif min == "minmax":
            self._viewer.set_autocut_params("minmax")
            self._viewer.auto_levels()
        else:
            if unit:
                print("ginga: ignoring scale unit %s" % unit, file=sys.stderr)

            self._viewer.cut_levels(min, max)

    def _show(self):
        """Show the requested display

        In this case, embed it in the notebook (equivalent to
        ``Display.get_viewer().show()``;
        see also Display.get_viewer().embed()

        N.b.  These command *must* be the last entry in their cell
        """
        return self._viewer.show()

    #
    # Zoom and Pan
    #
    def _zoom(self, zoomfac):
        """Zoom by specified amount"""
        self._viewer.scale_to(zoomfac, zoomfac)

    def _pan(self, colc, rowc):
        """Pan to (colc, rowc)"""
        self._viewer.set_pan(colc, rowc)

    def XXX_getEvent(self):
        """Listen for a key press, returning (key, x, y)"""
        raise RuntimeError("Write me")
        k = "?"
        x, y = self._viewer.get_pan()
        return GingaEvent(k, x, y)


class WcsAdaptorForGinga(object):
    """A class to adapt the LSST Wcs class for Ginga"""

    def __init__(self, wcs):
        self._wcs = wcs

    def pixtoradec(self, idxs, coords="data"):
        """Return (ra, dec) in degrees given a position in pixels"""
        ra, dec = self._wcs.pixelToSky(*idxs)

        return ra.asDegrees(), dec.asDegrees()

    def pixtosystem(self, idxs, system=None, coords="data"):
        """I'm not sure if ginga really needs this; equivalent to
        self.pixtoradec()"""
        return self.pixtoradec(idxs, coords=coords)

    def radectopix(self, ra_deg, dec_deg, coords="data", naxispath=None):
        """Return (x, y) in pixels given (ra, dec) in degrees"""
        return self._wcs.skyToPixel(ra_deg * afwGeom.degrees, dec_deg * afwGeom.degrees)
