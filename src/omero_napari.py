from functools import wraps

import omero.clients  # noqa
from omero.rtypes import rdouble, rint
from omero.model import PointI, ImageI, RoiI
from omero.gateway import BlitzGateway
from omero.model.enums import PixelsTypeint8, PixelsTypeuint8, PixelsTypeint16
from omero.model.enums import PixelsTypeuint16, PixelsTypeint32
from omero.model.enums import PixelsTypeuint32, PixelsTypefloat
from omero.model.enums import PixelsTypecomplex, PixelsTypedouble

from vispy.color import Colormap
import napari
from dask import delayed
import dask.array as da
from qtpy.QtWidgets import QPushButton

import numpy

import sys
from omero.cli import CLI
from omero.cli import BaseControl
from omero.cli import ProxyStringType

HELP = "Connect OMERO to the napari image viewer"

VIEW_HELP = "Usage: omero napari view Image:1"


def gateway_required(func):
    """
    Decorator which initializes a client (self.client),
    a BlitzGateway (self.gateway), and makes sure that
    all services of the Blitzgateway are closed again.
    """

    @wraps(func)
    def _wrapper(self, *args, **kwargs):
        self.client = self.ctx.conn(*args)
        self.gateway = BlitzGateway(client_obj=self.client)

        try:
            return func(self, *args, **kwargs)
        finally:
            if self.gateway is not None:
                self.gateway.close(hard=False)
                self.gateway = None
                self.client = None

    return _wrapper


class NapariControl(BaseControl):

    gateway = None
    client = None

    def _configure(self, parser):
        parser.add_login_arguments()
        sub = parser.sub()
        view = parser.add(sub, self.view, VIEW_HELP)

        obj_type = ProxyStringType("Image")

        view.add_argument("object", type=obj_type, help="Object to view")
        view.add_argument(
            "--eager",
            action="store_true",
            help=(
                "Use eager loading to load all planes immediately instead"
                "of lazy-loading each plane when needed"
            ),
        )

    @gateway_required
    def view(self, args):

        if isinstance(args.object, ImageI):
            image_id = args.object.id
            img = self._lookup(self.gateway, "Image", image_id)
            self.ctx.out("View image: %s" % img.name)

            with napari.gui_qt():
                viewer = napari.Viewer()

                add_buttons(viewer, img)

                load_omero_image(viewer, img, eager=args.eager)
                # add 'conn' and 'omero_image' to the viewer console
                viewer.update_console({"conn": self.gateway, "omero_image": img})

    def _lookup(self, gateway, type, oid):
        """Find object of type by ID."""
        gateway.SERVICE_OPTS.setOmeroGroup("-1")
        obj = gateway.getObject(type, oid)
        if not obj:
            self.ctx.die(110, "No such %s: %s" % (type, oid))
        return obj

def add_buttons(viewer, img):
    """
    Add custom buttons to the viewer UI
    """
    def handle_save_rois():
        save_rois(viewer, img)

    button = QPushButton("Save ROIs to OMERO")
    button.clicked.connect(handle_save_rois)
    viewer.window.add_dock_widget(button, name="Save OMERO", area="left")


def load_omero_image(viewer, image, eager=False):
    """
    Entry point - can be called to initially load an image
    from OMERO into the napari viewer

    :param  viewer:     napari viewer instance
    :param  image:      omero.gateway.ImageWrapper
    :param  eager:      If true, load all planes immediately
    """
    for c, channel in enumerate(image.getChannels()):
        print("loading channel %s:" % c)
        load_omero_channel(viewer, image, channel, c, eager)

    set_dims_defaults(viewer, image)
    set_dims_labels(viewer, image)


def load_omero_channel(viewer, image, channel, c_index, eager=False):
    """
    Loads a channel from OMERO image into the napari viewer

    :param  viewer:     napari viewer instance
    :param  image:      omero.gateway.ImageWrapper
    """
    if eager:
        data = get_data(image, c=c_index)
    else:
        data = get_data_lazy(image, c=c_index)
    # use current rendering settings from OMERO
    color = channel.getColor().getRGB()
    color = [r / 256 for r in color]
    cmap = Colormap([[0, 0, 0], color])
    win_start = channel.getWindowStart()
    win_end = channel.getWindowEnd()
    win_min = channel.getWindowMin()
    win_max = channel.getWindowMax()
    active = channel.isActive()
    z_scale = None
    # Z-scale for 3D viewing
    #  NB: This can cause unexpected behaviour
    #  https://forum.image.sc/t/napari-non-integer-step-size/31847
    #  And breaks viewer.dims.set_point(idx, position)
    # if image.getSizeZ() > 1:
    #     size_x = image.getPixelSizeX()
    #     size_z = image.getPixelSizeZ()
    #     if size_x is not None and size_z is not None:
    #         z_scale = [1, size_z / size_x, 1, 1]
    name = channel.getLabel()
    layer = viewer.add_image(
        data,
        blending="additive",
        colormap=("from_omero", cmap),
        scale=z_scale,
        name=name,
        visible=active,
    )
    layer._contrast_limits_range = [win_min, win_max]
    layer.contrast_limits = [win_start, win_end]
    return layer


def get_data(img, c=0):
    """
    Get n-dimensional numpy array of pixel data for the OMERO image.

    :param  img:        omero.gateway.ImageWrapper
    :c      int:        Channel index
    """
    sz = img.getSizeZ()
    st = img.getSizeT()
    # get all planes we need
    zct_list = [(z, c, t) for t in range(st) for z in range(sz)]
    pixels = img.getPrimaryPixels()
    planes = []
    for p in pixels.getPlanes(zct_list):
        # self.ctx.out(".", newline=False)
        planes.append(p)
    # self.ctx.out("")
    if sz == 1 or st == 1:
        return numpy.array(planes)
    # arrange plane list into 2D numpy array of planes
    z_stacks = []
    for t in range(st):
        z_stacks.append(numpy.array(planes[t * sz : (t + 1) * sz]))
    return numpy.array(z_stacks)


plane_cache = {}


def get_data_lazy(img, c=0):
    """
    Get n-dimensional dask array, with delayed reading from OMERO image.

    :param  img:        omero.gateway.ImageWrapper
    :c      int:        Channel index
    """
    sz = img.getSizeZ()
    st = img.getSizeT()
    plane_names = ["%s,%s,%s" % (z, c, t) for t in range(st) for z in range(sz)]

    def get_plane(plane_name):
        if plane_name in plane_cache:
            return plane_cache[plane_name]
        z, c, t = [int(n) for n in plane_name.split(",")]
        print("get_plane", z, c, t)
        pixels = img.getPrimaryPixels()
        p = pixels.getPlane(z, c, t)
        plane_cache[plane_name] = p
        return p

    pixels = img.getPrimaryPixels()
    pixelTypes = {
        PixelsTypeint8: numpy.int8,
        PixelsTypeuint8: numpy.uint8,
        PixelsTypeint16: numpy.int16,
        PixelsTypeuint16: numpy.uint16,
        PixelsTypeint32: numpy.int32,
        PixelsTypeuint32: numpy.uint32,
        PixelsTypefloat: numpy.float32,
        PixelsTypedouble: numpy.float64,
    }
    size_x = img.getSizeX()
    size_y = img.getSizeY()
    plane_shape = (size_y, size_x)
    pixelType = pixels.getPixelsType().value
    numpy_type = pixelTypes[pixelType]

    lazy_imread = delayed(get_plane)  # lazy reader
    lazy_arrays = [lazy_imread(pn) for pn in plane_names]
    dask_arrays = [
        da.from_delayed(delayed_reader, shape=plane_shape, dtype=numpy_type)
        for delayed_reader in lazy_arrays
    ]
    # Stack into one large dask.array
    if sz == 1 or st == 1:
        return da.stack(dask_arrays, axis=0)

    z_stacks = []
    for t in range(st):
        z_stacks.append(da.stack(dask_arrays[t * sz : (t + 1) * sz], axis=0))
    stack = da.stack(z_stacks, axis=0)
    return stack


def set_dims_labels(viewer, image):
    """
    Set labels on napari viewer dims, based on
    dimensions of OMERO image

    :param  viewer:     napari viewer instance
    :param  image:      omero.gateway.ImageWrapper
    """
    # dims (t, z, y, x) for 5D image
    dims = []
    if image.getSizeT() > 1:
        dims.append("T")
    if image.getSizeZ() > 1:
        dims.append("Z")

    for idx, label in enumerate(dims):
        viewer.dims.set_axis_label(idx, label)


def set_dims_defaults(viewer, image):
    """
    Set Z/T slider index on napari viewer, according
    to default Z/T indecies of the OMERO image

    :param  viewer:     napari viewer instance
    :param  image:      omero.gateway.ImageWrapper
    """
    # dims (t, z, y, x) for 5D image
    dims = []
    if image.getSizeT() > 1:
        dims.append(image.getDefaultT())
    if image.getSizeZ() > 1:
        dims.append(image.getDefaultZ())

    for idx, position in enumerate(dims):
        viewer.dims.set_point(idx, position)


def save_rois(viewer, image):
    """
    Usage: In napari, open console...
    >>> from omero_napari import *
    >>> save_rois(viewer, omero_image)
    """
    conn = image._conn

    for layer in viewer.layers:
        if layer.name.startswith("Points"):
            for p in layer.data:
                z = p[0]
                y = p[1]
                x = p[2]

                point = PointI()
                point.x = rdouble(x)
                point.y = rdouble(y)
                point.theZ = rint(z)
                point.theT = rint(0)
                roi = create_roi(conn, image.id, [point])
                print("Created ROI: %s" % roi.id.val)

    conn.close()


def create_roi(conn, img_id, shapes):
    updateService = conn.getUpdateService()
    roi = RoiI()
    roi.setImage(ImageI(img_id, False))
    for shape in shapes:
        roi.addShape(shape)
    return updateService.saveAndReturnObject(roi)


# Register omero_napari as an OMERO CLI plugin
if __name__ == "__main__":
    cli = CLI()
    cli.register("napari", NapariControl, HELP)
    cli.invoke(sys.argv[1:])
