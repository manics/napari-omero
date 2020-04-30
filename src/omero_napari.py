from functools import wraps
from datetime import datetime

import omero.clients  # noqa
from omero.rtypes import rdouble, rint, rstring
from omero.model import PointI, ImageI, RoiI, LineI, \
    PolylineI, PolygonI, RectangleI, EllipseI
from omero.gateway import BlitzGateway, PixelsWrapper
from omero.model.enums import PixelsTypeint8, PixelsTypeuint8, PixelsTypeint16
from omero.model.enums import PixelsTypeuint16, PixelsTypeint32
from omero.model.enums import PixelsTypeuint32, PixelsTypefloat
from omero.model.enums import PixelsTypedouble

from vispy.color import Colormap
import napari
from napari.layers.points.points import Points as points_layer
from napari.layers.shapes.shapes import Shapes as shapes_layer
from napari.layers.labels.labels import Labels as labels_layer
from dask import delayed
import dask.array as da
from fsspec.implementations.http import HTTPFileSystem
import zarr
from qtpy.QtWidgets import QPushButton

import numpy

import sys
import os
import s3fs
from omero.cli import CLI
from omero.cli import BaseControl
from omero.cli import ProxyStringType

import logging
# DEBUG logging for s3fs so we can track remote calls
logging.basicConfig(level=logging.INFO)
logging.getLogger('s3fs').setLevel(logging.DEBUG)

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
        view.add_argument(
            "--zarr",
            action="store_true",
            help=("Use remote Zarr data")
        )
        view.add_argument(
            "--resolutions",
            help=("Optional: specify multiscale resolutions for zarr image. E.g. '0,1' or '1-3'."
                "'0' is the highest resolution. Default is to use all available resolutions")
        )
        view.add_argument(
            "--endpoint-url", "--endpoint_url", type=str,
            default="https://minio-dev.openmicroscopy.org/",
            help=("Specify S3 url endpoint to load zarr files")
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

                load_omero_image(viewer, img, args)
                # add 'conn' and 'omero_image' to the viewer console
                viewer.update_console({"conn": self.gateway,
                                       "omero_image": img})

    def _lookup(self, gateway, type, oid):
        """Find object of type by ID."""
        gateway.SERVICE_OPTS.setOmeroGroup("-1")
        obj = gateway.getObject(type, oid)
        if not obj:
            self.ctx.die(110, "No such %s: %s" % (type, oid))
        return obj


# Register omero_napari as an OMERO CLI plugin
if __name__ == "__main__":
    cli = CLI()
    cli.register("napari", NapariControl, HELP)
    cli.invoke(sys.argv[1:])


def add_buttons(viewer, img):
    """
    Add custom buttons to the viewer UI
    """
    def handle_save_rois():
        save_rois(viewer, img)

    button = QPushButton("Save ROIs to OMERO")
    button.clicked.connect(handle_save_rois)
    viewer.window.add_dock_widget(button, name="Save OMERO", area="left")


def load_omero_image(viewer, image, args):
    """
    Entry point - can be called to initially load an image
    from OMERO into the napari viewer

    :param  viewer:     napari viewer instance
    :param  image:      omero.gateway.ImageWrapper
    :param  eager:      If true, load all planes immediately
    :param  use_zarr:   If true, load zarr
    """

    n = datetime.now()
    eager=args.eager
    use_zarr=args.zarr
    if use_zarr:

        cache_size_mb = 2048

        # group '0' is for highest resolution pyramid
        # see https://github.com/ome/omero-ms-zarr/pull/8/files#diff-958e7270f96f5407d7d980f500805b1b

        # TODO: try to look-up resolutions from zarr metadata...
        s3 = s3fs.S3FileSystem(
            anon=True,
            client_kwargs={
                'endpoint_url': args.endpoint_url,
            },
        )
        # top-level
        root_url = 'idr/zarr/v0.1/%s.zarr/' % image.id
        store = s3fs.S3Map(root=root_url, s3=s3, check=False)
        root = zarr.group(store=store)
        root_attrs = root.attrs.asdict()
        # {'multiscales': [{'datasets': [{'path': '0'}, {'path': '1'}], 'version': '0.1'}]}
        print('root_attrs', root.attrs.asdict())

        resolutions = ['0']
        if args.resolutions is not None:
            try:
                if '-' in args.resolutions:
                    # E.g. 0-2 => [0,1,2]
                    start, stop = args.resolutions.split('-', 1)
                    resolutions = list(range(int(start), int(stop) + 1))
                elif ',' in args.resolutions:
                    # E.g. 1,2 => [1,2]
                    resolutions = [int(r) for r in args.resolutions.split(',')]
                else:
                    resolutions = [int(args.resolutions)]
            except ValueError:
                print('--resolutions should be number, range (0-2) or 0,1')
                raise
        elif 'multiscales' in root_attrs:
            resolutions = [d['path'] for d in root_attrs['multiscales'][0]['datasets']]
        print('resolutions', resolutions)

        pyramid = []
        for resolution in resolutions:
            root = 'idr/zarr/v0.1/%s.zarr/%s/' % (image.id, resolution)
            store = s3fs.S3Map(root=root, s3=s3, check=False)
            cached_store = zarr.LRUStoreCache(store, max_size=(cache_size_mb * 2**20))
            # data.shape is (t, c, z, y, x) by convention
            data = da.from_zarr(cached_store)
            pyramid.append(data)
        is_pyramid = len(pyramid) > 1

        # Each channel is a separate 'image' in napari (different rendering settings etc.)
        for c, channel in enumerate(image.getChannels()):
            # slice to get channel
            ch_data = [data[:, c, :, :, :] for data in pyramid]
            if not is_pyramid:
                ch_data = ch_data[0]
            load_omero_channel(viewer, image, channel, c, ch_data,
                               is_pyramid=is_pyramid)

    else:
        for c, channel in enumerate(image.getChannels()):
            print("loading channel %s:" % c)
            if eager:
                data = get_data(image, c=c)
            else:
                data = get_data_lazy(image, c=c)
            load_omero_channel(viewer, image, channel, c, data)

    # If lazy-loading data. This will load data for default Z/T positions
    set_dims_defaults(viewer, image)
    set_dims_labels(viewer, image)

    print("time to load_omero_image(): ", (datetime.now() - n).total_seconds())


def load_omero_channel(viewer, image, channel, c_index, data, is_pyramid=False):
    """
    Loads a channel from OMERO image into the napari viewer

    :param  viewer:     napari viewer instance
    :param  image:      omero.gateway.ImageWrapper
    """
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
    print('window', c_index, win_start, win_end)
    layer = viewer.add_image(
        data,
        blending="additive",
        colormap=("from_omero", cmap),
        scale=z_scale,
        name=name,
        visible=active,
        contrast_limits = [win_start, win_end],
        is_pyramid=is_pyramid,
    )
    # TODO: we want to set the contrast_limits in add_image() so that
    # we don't load extra data to calculate this.
    # BUT, this gets ignored if you include the line below
    # layer._contrast_limits_range = [win_min, win_max]
    return layer


def get_data(img, c=0):
    """
    Get 4D numpy array of pixel data, shape = (size_t, size_z, size_y, size_x)

    :param  img:        omero.gateway.ImageWrapper
    :c      int:        Channel index
    """
    size_z = img.getSizeZ()
    size_t = img.getSizeT()
    # get all planes we need in a single generator
    zct_list = [(z, c, t) for t in range(size_t) for z in range(size_z)]
    pixels = img.getPrimaryPixels()
    plane_gen = pixels.getPlanes(zct_list)

    t_stacks = []
    for t in range(size_t):
        z_stack = []
        for z in range(size_z):
            print('plane c:%s, t:%s, z:%s' % (c, t, z))
            z_stack.append(next(plane_gen))
        t_stacks.append(numpy.array(z_stack))
    return numpy.array(t_stacks)


plane_cache = {}


def get_data_lazy(img, c=0):
    """
    Get n-dimensional dask array, with delayed reading from OMERO image.

    :param  img:        omero.gateway.ImageWrapper
    :c      int:        Channel index
    """
    size_z = img.getSizeZ()
    size_t = img.getSizeT()

    def get_plane(plane_name):
        if plane_name in plane_cache:
            return plane_cache[plane_name]
        z, c, t = [int(n) for n in plane_name.split(",")]
        print("get_plane", z, c, t)
        pixels = img.getPrimaryPixels()
        p = pixels.getPlane(z, c, t)
        plane_cache[plane_name] = p
        return p

    size_x = img.getSizeX()
    size_y = img.getSizeY()
    plane_shape = (size_y, size_x)
    numpy_type = get_numpy_pixel_type(img)

    lazy_reader = delayed(get_plane)  # lazy reader

    t_stacks = []
    for t in range(size_t):
        z_stack = []
        for z in range(size_z):
            plane_name = "%s,%s,%s" % (z, c, t)
            lazy_plane = da.from_delayed(lazy_reader(plane_name), shape=plane_shape, dtype=numpy_type)
            z_stack.append(lazy_plane)
        t_stacks.append(da.stack(z_stack))
    return da.stack(t_stacks)


def get_numpy_pixel_type(image):
    pixels = image.getPrimaryPixels()
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
    pixelType = pixels.getPixelsType().value
    return pixelTypes.get(pixelType, None)


def set_dims_labels(viewer, image):
    """
    Set labels on napari viewer dims, based on
    dimensions of OMERO image

    :param  viewer:     napari viewer instance
    :param  image:      omero.gateway.ImageWrapper
    """
    # dims (t, z, y, x) for 5D image
    dims = 'TZ'

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
    if image.getSizeT() > 1:
        viewer.dims.set_point(0, image.getDefaultT())
    if image.getSizeZ() > 1:
        viewer.dims.set_point(1, image.getDefaultZ())


def save_rois(viewer, image):
    """
    Usage: In napari, open console...
    >>> from omero_napari import *
    >>> save_rois(viewer, omero_image)
    """
    conn = image._conn

    for layer in viewer.layers:
        if type(layer) == points_layer:
            for p in layer.data:
                point = create_omero_point(p)
                roi = create_roi(conn, image.id, [point])
                print("Created ROI: %s" % roi.id.val)
        elif type(layer) == shapes_layer:
            if len(layer.data) == 0 or len(layer.shape_type) == 0:
                continue
            shape_types = layer.shape_type
            if isinstance(shape_types, str):
                shape_types = [layer.shape_type
                               for t in range(len(layer.data))]
            for shape_type, data in zip(shape_types, layer.data):
                shape = create_omero_shape(shape_type, data)
                if shape is not None:
                    roi = create_roi(conn, image.id, [shape])
                    print("Created ROI: %s" % roi.id.val)
        elif type(layer) == labels_layer:
            print('Saving Labels not supported')


def get_x(coordinate):
    return coordinate[-1]


def get_y(coordinate):
    return coordinate[-2]


def get_t(coordinate):
    return coordinate[0]


def get_z(coordinate):
    return coordinate[1]


def create_omero_point(data):
    point = PointI()
    point.x = rdouble(get_x(data))
    point.y = rdouble(get_y(data))
    point.theZ = rint(get_z(data))
    point.theT = rint(get_t(data))
    return point


def create_omero_shape(shape_type, data):
    # "line", "path", "polygon", "rectangle", "ellipse"
    # NB: assume all points on same plane.
    # Use first point to get Z and T index
    z_index = get_z(data[0])
    t_index = get_t(data[0])
    shape = None
    if shape_type == "line":
        shape = LineI()
        shape.x1 = rdouble(get_x(data[0]))
        shape.y1 = rdouble(get_y(data[0]))
        shape.x2 = rdouble(get_x(data[1]))
        shape.y2 = rdouble(get_y(data[1]))
    elif shape_type == "path" or shape_type == "polygon":
        shape = PolylineI() if shape_type == "path" else PolygonI()
        # points = "10,20, 50,150, 200,200, 250,75"
        points = ["%s,%s" % (get_x(d), get_y(d)) for d in data]
        shape.points = rstring(", ".join(points))
    elif shape_type == "rectangle" or shape_type == "ellipse":
        # corners go anti-clockwise starting top-left
        x1 = get_x(data[0])
        x2 = get_x(data[1])
        x3 = get_x(data[2])
        x4 = get_x(data[3])
        y1 = get_y(data[0])
        y2 = get_y(data[1])
        y3 = get_y(data[2])
        y4 = get_y(data[3])
        if shape_type == "rectangle":
            # Rectangle not rotated
            if x1 == x2:
                shape = RectangleI()
                # TODO: handle 'updside down' rectangle x3 < x1
                shape.x = rdouble(x1)
                shape.y = rdouble(y1)
                shape.width = rdouble(x3 - x1)
                shape.height = rdouble(y2 - y1)
            else:
                # Rotated Rectangle - save as Polygon
                shape = PolygonI()
                points = "%s,%s, %s,%s, %s,%s, %s,%s" % (
                    x1, y1, x2, y2, x3, y3, x4, y4
                )
                shape.points = rstring(points)
        elif shape_type == "ellipse":
            # Ellipse not rotated (ignore floating point rouding)
            if int(x1) == int(x2):
                shape = EllipseI()
                shape.x = rdouble((x1 + x3) / 2)
                shape.y = rdouble((y1 + y2) / 2)
                shape.radiusX = rdouble(abs(x3 - x1) / 2)
                shape.radiusY = rdouble(abs(y2 - y1) / 2)
            else:
                # TODO: Need to calculate transformation matrix
                print("Rotated Ellipse not yet supported!")

    if shape is not None:
        shape.theZ = rint(z_index)
        shape.theT = rint(t_index)
    return shape


def create_roi(conn, img_id, shapes):
    updateService = conn.getUpdateService()
    roi = RoiI()
    roi.setImage(ImageI(img_id, False))
    for shape in shapes:
        roi.addShape(shape)
    return updateService.saveAndReturnObject(roi)


class NonCachedPixelsWrapper(PixelsWrapper):
    """Extend gateway.PixelWrapper to override _prepareRawPixelsStore."""

    def _prepareRawPixelsStore(self):
        """
        Creates RawPixelsStore and sets the id etc

        This overrides the superclass behaviour to make sure that
        we don't re-use RawPixelStore in multiple processes since
        the Store may be closed in 1 process while still needed elsewhere.
        This is needed when napari requests may planes simultaneously,
        e.g. when switching to 3D view.
        """
        ps = self._conn.c.sf.createRawPixelsStore()
        ps.setPixelsId(self._obj.id.val, True, self._conn.SERVICE_OPTS)
        return ps


omero.gateway.PixelsWrapper = NonCachedPixelsWrapper
# Update the BlitzGateway to use our NonCachedPixelsWrapper
omero.gateway.refreshWrappers()
