from django.contrib.gis.db import models
from django.http import Http404, HttpResponse

from boundaries.base_views import (ModelListView, ModelDetailView,
                                        ModelGeoListView, ModelGeoDetailView)
from boundaries.models import BoundarySet, Boundary, app_settings

# Imports for generating map tiles
from django.contrib.gis.geos import Point, Polygon, MultiPolygon
from django.contrib.gis.gdal import CoordTransform, SpatialReference
from django.db import connections
from django.shortcuts import render_to_response, get_object_or_404
from django.template import RequestContext
from django.views.decorators.cache import cache_control
import math
try:
    import cairo 
    from StringIO import StringIO
    has_imaging_library = True
except ImportError:
    has_imaging_library = False

class BoundarySetListView(ModelListView):
    """ e.g. /boundary-set/ """

    filterable_fields = ['name', 'domain']

    model = BoundarySet

class BoundarySetDetailView(ModelDetailView):
    """ e.g. /boundary-set/federal-electoral-districts/ """

    model = BoundarySet

    def get_object(self, request, qs, slug):
        try:
            return qs.get(slug=slug)
        except BoundarySet.DoesNotExist:
            raise Http404

class BoundaryListView(ModelGeoListView):
    """ e.g. /boundary/federal-electoral-districts/
    or /boundary/federal-electoral-districts/centroid """

    filterable_fields = ['external_id', 'name']
    allowed_geo_fields = ('shape', 'simple_shape', 'centroid')
    default_geo_filter_field = 'shape'
    model = Boundary

    def filter(self, request, qs):
        qs = super(BoundaryListView, self).filter(request, qs)

        if 'intersects' in request.GET:
            (set_slug, slug) = request.GET['intersects'].split('/')
            try:
                shape = Boundary.objects.filter(slug=slug, set=set_slug).values_list('shape', flat=True)[0]
            except IndexError:
                raise Http404
            qs = qs.filter(models.Q(shape__covers=shape) | models.Q(shape__overlaps=shape))

        if 'touches' in request.GET:
            (set_slug, slug) = request.GET['touches'].split('/')
            try:
                shape = Boundary.objects.filter(slug=slug, set=set_slug).values_list('shape', flat=True)[0]
            except IndexError:
                raise Http404
            qs = qs.filter(shape__touches=shape)

        if 'sets' in request.GET:
            set_slugs = request.GET['sets'].split(',')
            qs = qs.filter(set__in=set_slugs)

        return qs

    def get_qs(self, request, set_slug=None):
        qs = super(BoundaryListView, self).get_qs(request)
        if set_slug:
            if not BoundarySet.objects.filter(slug=set_slug).exists():
                raise Http404
            return qs.filter(set=set_slug)
        return qs

    def get_related_resources(self, request, qs, meta):
        r = super(BoundaryListView, self).get_related_resources(request, qs, meta)
        if meta['total_count'] == 0 or meta['total_count'] > app_settings.MAX_GEO_LIST_RESULTS:
            return r

        geo_url = request.path + r'%s'
        if request.META['QUERY_STRING']:
            geo_url += '?' + request.META['QUERY_STRING'].replace('%', '%%')

        r.update(
            shapes_url=geo_url % 'shape',
            simple_shapes_url=geo_url % 'simple_shape',
            centroids_url=geo_url % 'centroid'
        )
        return r


class BoundaryObjectGetterMixin(object):

    model = Boundary

    def get_object(self, request, qs, set_slug, slug):
        try:
            return qs.get(slug=slug, set=set_slug)
        except Boundary.DoesNotExist:
            raise Http404

class BoundaryDetailView(ModelDetailView, BoundaryObjectGetterMixin):
    """ e.g. /boundary/federal-electoral-districts/outremont/ """

    def __init__(self):
        super(BoundaryDetailView, self).__init__()
        self.base_qs = self.base_qs.defer('shape', 'simple_shape', 'centroid', 'extent')

class BoundaryGeoDetailView(ModelGeoDetailView, BoundaryObjectGetterMixin):
    """ e.g /boundary/federal-electoral-districts/outremont/shape """

    allowed_geo_fields = ('shape', 'simple_shape', 'centroid', 'extent')

def boundaries_map(request, set_slug, boundary_slug):
    bs = get_object_or_404(BoundarySet, slug=set_slug)
    bb = get_object_or_404(Boundary, set=bs, slug=boundary_slug) if boundary_slug else None
    return render_to_response('boundaries/map_test.html',
      { "boundaryset": bs, "boundary": bb },
      context_instance=RequestContext(request))

@cache_control(public=True, max_age=60*60*24*3) # ask to be cached for 3 days
def boundaries_map_tiles(request, set_slug, boundary_slug):
    if not has_imaging_library: raise Http404("Cairo is not available.")
    
    # Load basic parameters.
    try:
        size = int(request.GET.get('size', '256'))
        if size not in (256, 512, 1024): raise ValueError()
        
        srs = int(request.GET.get('srs', '3857'))
    except ValueError:
        raise Http404("Invalid parameter.")
        
    # Define coordinate transformations between the database and the
    # output SRS.
    geometry_field = Boundary._meta.get_field_by_name('shape')[0]
    SpatialRefSys = connections['default'].ops.spatial_ref_sys()
    out_srs = SpatialRefSys.objects.get(srid=srs).srs
    
    if srs == 3857:
        # When converting to EPSG:3857, the Google 'web mercator' projection,
        # the transformation does not work right when the database is set to
        # WGS84 (EPSG:4326).
        #
        # Some guy writes:
        #    I read that "web mercator" uses WGS84 coordinates but
        #    consider them as if they where spherical coordinates.
        #    Due to the difference between a geodetic and a geocentric
        #    latitude (See Wikipedia about the latitude), the latitude
        #    values will not be the same on an ellipsoid or on a sphere.
        #    I found that EPSG:4055 is the code for spherical coordinates
        #    on a sphere based on WGS84.
        # http://gis.stackexchange.com/questions/2904/how-to-georeference-a-web-mercator-tile-correctly-using-gdal
        #
        # Assume the database is WGS84 and specify EPSG:4055 so that the
        # transformation believes they are spherical coordinates. Is this
        # a GDAL bug? Don't know. But this does the trick.
        db_srs = SpatialRefSys.objects.get(srid=4055).srs
    else:
        db_srs = SpatialRefSys.objects.get(srid=geometry_field.srid).srs
    
    # Get the bounding box for the tile, in the SRS of the output.
    
    if "tile_zoom" in request.GET or True:
        try:
            tile_x = int(request.GET.get('tile_x', '0'))
            tile_y = int(request.GET.get('tile_y', '0'))
            tile_zoom = int(request.GET.get('tile_zoom', '0'))
        except ValueError:
            raise Http404("Invalid parameter.")
            
        p = Point( (180.0, 0.0), srid=db_srs.srid )
        p.transform(out_srs)
        world_left = -p[0]
        world_top = -world_left
        world_size = p[0] * 2.0
        tile_world_size = world_size / math.pow(2.0, tile_zoom)
        
        p1 = Point( (world_left + tile_world_size*tile_x, world_top - tile_world_size*tile_y) )
        p2 = Point( (world_left + tile_world_size*(tile_x+1), world_top - tile_world_size*(tile_y+1)) )
        bbox = Polygon( ((p1[0], p1[1]),(p2[0], p1[1]),(p2[0], p2[1]),(p1[0], p2[1]),(p1[0], p1[1])), srid=out_srs.srid )
        
    # A function to convert world coordinates in the output SRS into
    # pixel coordinates.
       
    blon1, blat1, blon2, blat2 = bbox.extent
    bx = float(size)/(blon2-blon1)
    by = float(size)/(blat2-blat1)
    def viewport(coord):
        # Convert the world coordinates to image coordinates according to the bounding box
        # (in output SRS).
        return float(coord[0] - blon1)*bx, (size-1) - float(coord[1] - blat1)*by

    # Convert the bounding box to the database SRS.

    db_bbox = bbox.transform(db_srs, clone=True)
    
    # What is the width of a pixel in the database SRS? If it is smaller than
    # SIMPLE_SHAPE_TOLERANCE, load the simplified geometry from the database.
    
    shape_field = 'shape'
    pixel_width = (db_bbox.extent[2]-db_bbox.extent[0]) / size / 2
    if pixel_width > app_settings.SIMPLE_SHAPE_TOLERANCE:
        shape_field = 'simple_shape'

    # Query for any boundaries that intersect the bounding box.
    
    boundaries = Boundary.objects.filter(set__slug=set_slug, shape__intersects=db_bbox)\
        .values("name", "label_point", "color", shape_field)
    if boundary_slug: boundaries = boundaries.filter(slug=boundary_slug)
    
    if len(boundaries) == 0:
        if False:
            # Google is OK getting 404s but OpenLayers isn't.
            raise Http404("No boundaries here.")
        else:
            # Send a 1x1 transparent PNG.
            im = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
            ctx = cairo.Context(im)
            buf = StringIO()
            im.write_to_png(buf)
            v = buf.getvalue()
            r = HttpResponse(v, content_type='image/png')
            r["Content-Length"] = len(v)
            return r
    
    # Create the image buffer.
    im = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(im)
    ctx.select_font_face(app_settings.MAP_LABEL_FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    
    def max_extent(shape):
        a, b, c, d = shape.extent
        return max(c-a, d-b)
    
    # Transform the boundaries to output coordinates.
    draw_shapes = []
    for bdry in boundaries:
        shape = bdry[shape_field]
        
        # Simplify to the detail that could be visible in the output. Although
        # simplification may be a little expensive, drawing a more complex
        # polygon is even worse.
        shape = shape.simplify(pixel_width, preserve_topology=True)
        
        # Make sure the results are all MultiPolygons for consistency.
        if shape.__class__.__name__ == 'Polygon':
            shape = MultiPolygon((shape,), srid=db_srs.srid)
        else:
            # Be sure to override SRS (for Google, see above). This code may
            # never execute?
            shape = MultiPolygon(list(shape), srid=db_srs.srid)

        # Is this shape too small to be visible?
        ext_dim = max_extent(shape)
        if ext_dim < pixel_width:
            continue

        # Convert the shape to the output SRS.
        shape.transform(out_srs)
        
        draw_shapes.append( (bdry, shape, ext_dim) )
        
    # Draw shading, for each linear ring of each polygon in the multipolygon.
    for bdry, shape, ext_dim in draw_shapes:
        if not bdry["color"]: continue
        for polygon in shape:
            for ring in polygon: # should just be one since no shape should have holes?
                # We have to 'eval' the color because we used .values() to pull the
                # value, so the JSON field won't decode it for us.
                color = eval(bdry["color"])
                if isinstance(color, (tuple, list)):
                    # Specify a 3-tuple (or list) for a solid RGB color w/ default
                    # alpha. RGB are in the range 0-255.
                    if len(color) == 3:
                        ctx.set_source_rgba(*[f/255.0 for f in (color + [60])])
                        
                    # Specify a 4-tuple (or list) for a solid RGB color with alpha
                    # specified as the fourth component. Values in range 0-255.
                    elif len(color) == 4:
                        ctx.set_source_rgba(*[f/255.0 for f in color])
                        
                    else:
                        continue # Invalid length.
                        
                elif isinstance(color, dict):
                    # Specify a dict of the form { "color1": (R,G,B), "color2": (R,G,B) } to
                    # create a solid fill of color1 plus smaller stripes of color2.
                    pat = cairo.LinearGradient(0.0, 0.0, size, size)
                    for x in xrange(0,size, 32): # divisor of the size so gradient ends at the end
                        pat.add_color_stop_rgba(*([float(x)/size] + [f/255.0 for f in color["color1"]] + [.3]))
                        pat.add_color_stop_rgba(*([float(x+28)/size] + [f/255.0 for f in color["color1"]] + [.3]))
                        pat.add_color_stop_rgba(*([float(x+28)/size] + [f/255.0 for f in color["color2"]] + [.4]))
                        pat.add_color_stop_rgba(*([float(x+32)/size] + [f/255.0 for f in color["color2"]] + [.4]))
                    ctx.set_source(pat)
                else:
                    continue # Unknown color data structure.
                ctx.new_path()
                for pt in ring.coords:
                    ctx.line_to(*viewport(pt))
                ctx.fill()
                
    # Draw outlines, for each linear ring of each polygon in the multipolygon.
    for bdry, shape, ext_dim in draw_shapes:
        if ext_dim < pixel_width * 3: continue # skip outlines if too small
        for polygon in shape:
            for ring in polygon: # should just be one since no shape should have holes?
                ctx.new_path()
                for pt in ring.coords:
                    ctx.line_to(*viewport(pt))
                if ext_dim < pixel_width * 60:
                    ctx.set_line_width(1)
                else:
                    ctx.set_line_width(2.5)
                ctx.set_source_rgba(.3,.3,.3, .75)  # grey, semi-transparent
                ctx.stroke_preserve()
                
    # Draw labels.
    for bdry, shape, ext_dim in draw_shapes:
        if ext_dim < pixel_width * 20: continue
        
        # Get the location of the label stored in the database, or fall back to
        # GDAL routine point_on_surface to get a point quickly.
        if bdry["label_point"]:
            # Override the SRS on the point (for Google, see above). Then transform
            # it to world coordinates.
            pt = Point(tuple(bdry["label_point"]), srid=db_srs.srid)
            pt.transform(out_srs)
        else:
            try:
                pt = bbox.intersection(shape).point_on_surface
            except:
                # Don't know why this would fail. Bad geometry of some sort.
                # But we really don't want to leave anything unlabeled so
                # try the center of the bounding box.
                pt = bbox.centroid
                if not shape.contains(pt):
                    continue
        
        # Transform to world coordinates and ensure it is within the bounding box.
        if not bbox.contains(pt):
            # If it's not in the bounding box and the shape occupies most of this
            # bounding box, try moving the point to somewhere in the current tile.
            try:
                inters = bbox.intersection(shape)
                if inters.area < bbox.area/3: continue
                pt = inters.point_on_surface
            except:
                continue
        pt = viewport(pt)
        
        txt = bdry["name"]
        if ext_dim > size * pixel_width:
            ctx.set_font_size(18)
        else:
            ctx.set_font_size(12)
        x_off, y_off, tw, th = ctx.text_extents(txt)[:4]
        
        # Is it within the rough bounds of the shape and definitely the bounds of this tile?
        if tw < ext_dim/pixel_width/5 and th < ext_dim/pixel_width/5 \
            and pt[0]-x_off-tw/2-4 > 0 and pt[1]-th-4 > 0 and pt[0]-x_off+tw/2+7 < size and pt[1]+6 < size:
            # Draw the background rectangle behind the text.
            ctx.set_source_rgba(0,0,0,.55)  # black, some transparency
            ctx.new_path()
            ctx.line_to(pt[0]-x_off-tw/2-4,pt[1]-th-4)
            ctx.rel_line_to(tw+9, 0)
            ctx.rel_line_to(0, +th+8)
            ctx.rel_line_to(-tw-9, 0)
            ctx.fill()
            
            # Now a drop shadow (also is partially behind the first rectangle).
            ctx.set_source_rgba(0,0,0,.3)  # black, some transparency
            ctx.new_path()
            ctx.line_to(pt[0]-x_off-tw/2-4,pt[1]-th-4)
            ctx.rel_line_to(tw+11, 0)
            ctx.rel_line_to(0, +th+10)
            ctx.rel_line_to(-tw-11, 0)
            ctx.fill()
            
            # Draw the text.
            ctx.set_source_rgba(1,1,1,1)  # white
            ctx.move_to(pt[0]-x_off-tw/2,pt[1])
            ctx.show_text(txt)
                

    # Convert the image buffer to raw bytes.
    buf = StringIO()
    im.write_to_png(buf)
    v = buf.getvalue()
    
    # Form the response.
    r = HttpResponse(v, content_type='image/png')
    r["Content-Length"] = len(v)
    
    return r


