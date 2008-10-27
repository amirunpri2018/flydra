from __future__ import division
from __future__ import with_statement

if 1:
    # deal with old files, forcing to numpy
    import tables.flavor
    tables.flavor.restrict_flavors(keep=['numpy'])
import sets, os, sys, math, contextlib, collections, warnings

import pkg_resources
import numpy as np
import tables as PT
from optparse import OptionParser
import flydra.reconstruct as reconstruct
import motmot.ufmf.ufmf as ufmf
import flydra.a2.utils as utils
import flydra.analysis.result_utils as result_utils
import core_analysis
import scipy.misc.pilutil
import motmot.FastImage.FastImage as FastImage
import motmot.realtime_image_analysis.realtime_image_analysis \
       as realtime_image_analysis

import cairo
import benu

@contextlib.contextmanager
def openFileSafe(*args,**kwargs):
    result = PT.openFile(*args,**kwargs)
    try:
        yield result
    finally:
        result.close()

def get_cam_id_from_filename(filename, all_cam_ids):
    # guess cam_id
    n = 0
    found_cam_id = None
    for cam_id in all_cam_ids:
        if cam_id in filename:
            n+=1
            if found_cam_id is not None:
                raise ValueError('cam_id found more than once in filename')
            found_cam_id = cam_id
    return found_cam_id

def clear_col(dest_table, colname):
    if 0:
        objcol = dest_table._getColumnInstance(colname)
        descr = [objcol._v_parent._v_nestedDescr[objcol._v_pos]]
        dtype = descr[0][1]

        nancol = np.ones( (dest_table.nrows,), dtype=dtype)
        #recarray = np.rec.array( nancol, dtype=descr)

        dest_table.modifyColumn(column=nancol, colname='x')
        dest_table.flush()
    else:
        warnings.warn('slow implementation of column clearing')
        for row in dest_table:
            row[colname] = np.nan
            row.update()

def plot_image_subregion(raw_im, mean_im, absdiff_im,
                         roiradius, fname, user_coords,
                         scale=4.0, view='orig',
                         extras=None):
    if extras is None:
        extras = {}
    output_ext = os.path.splitext(fname)[1].lower()

    roisize = 2*roiradius
    imtypes=['raw','absdiff','mean']
    margin = 10
    square_edge = roisize*scale
    width=int(round(len(imtypes)*square_edge + (len(imtypes)+1)*margin))
    height=int(round(square_edge+2*margin))
    if output_ext == '.pdf':
        output_surface = cairo.PDFSurface(fname,
                                          width, height)
    elif output_ext == '.svg':
        output_surface = cairo.SVGSurface(fname,
                                          width, height)
    elif output_ext == '.png':
        output_surface = cairo.ImageSurface(
            cairo.FORMAT_ARGB32,width, height)
    else:
        raise ValueError('unknown output extension %s'%output_ext)

    ctx = cairo.Context(output_surface)

    # fill with white
    ctx.set_source_rgb(1,1,1)
    ctx.rectangle(0,0,width,height)
    ctx.fill()

    user_l, user_b, user_r, user_t = user_coords

    # setup transform
    #   calculate image boundary (user coords)

    for im_idx,im in enumerate(imtypes):
        if im=='raw':
            display_im = raw_im
        elif im=='mean':
            display_im = mean_im
        elif im=='absdiff':
            display_im = np.clip( 5*absdiff_im,0,255)
        # set transform - make a patch of the cairo
        # device be addressed with our image space
        # coords
        device_l = (im_idx+1)*margin + im_idx*square_edge
        device_b = margin

        ctx.identity_matrix() # reset
        if view=='orig':
            matrix = cairo.Matrix(xx=scale,
                                  yx=0,
                                  xy=0,
                                  yy=scale,
                                  x0=(device_l-scale*user_l),
                                  y0=(device_b-scale*user_b),
                                  )
        elif view=='rot -90':
            matrix = cairo.Matrix(xx=0,
                                  yx=scale,
                                  xy=scale,
                                  yy=0,
                                  x0=(device_l-scale*user_b),
                                  y0=(device_b-scale*user_l),
                                  )
        elif view=='rot 180':
            matrix = cairo.Matrix(xx=-scale,
                                  yx=0,
                                  xy=0,
                                  yy=-scale,
                                  x0=(device_l+scale*user_r),
                                  y0=(device_b+scale*user_t),
                                  )
        else:
            raise ValueError("unknown view '%s'"%view)
        ctx.set_matrix(matrix)
        ## print 'device_l-user_l, device_b-user_b',device_l-user_l, device_b-user_b
        ## #ctx.translate(device_l-user_l, device_b-user_b)
        ## if scale!= 1.0:
        ##     ctx.scale( scale, scale )
        ##     #raise NotImplementedError('')
        ## ctx.translate(device_l-user_l, device_b-user_b)
        ## #print 'square_edge/roisize, square_edge/roisize',square_edge/roisize, square_edge/roisize
        ## #ctx.scale( roisize/square_edge, square_edge/roisize)

        if 1:
            in_surface = benu.numpy2cairo(display_im)
            ctx.rectangle(user_l,user_b,display_im.shape[1],display_im.shape[0])
            if 1:
                ctx.save()
                ctx.set_source_surface(in_surface,user_l,user_b)
                ctx.paint()
                ctx.restore()
            else:
                ctx.set_source_rgb(0,.3,0)
                ctx.fill()

        if 0:
            ctx.move_to(user_l,user_b)

            ctx.line_to(user_r,user_b)
            ctx.line_to(user_r,user_t)
            ctx.line_to(user_l,user_t)
            ctx.line_to(user_l,user_b)
            ctx.close_path()
            ctx.set_source_rgb(0,1,0)
            ctx.fill()

            ctx.move_to(user_l+5,user_b+5)

            ctx.line_to(user_r-40,user_b+5)
            ctx.line_to(user_r-40,user_t-40)
            ctx.line_to(user_l+5,user_t-40)
            ctx.line_to(user_l+5,user_b+5)
            ctx.close_path()
            ctx.set_source_rgb(0,0,1)
            ctx.fill()


    if output_ext == '.png':
        output_surface.write_to_png(fname)
    else:
        ctx.show_page()
        output_surface.finish()

def running_average( image_framenumbers, ims,
                     im_coords, camn_pt_no_array, N=5,
                     min_N=5 ):
    assert np.all( (image_framenumbers[1:]-image_framenumbers[:-1])
                   > 0 )
    all_framenumbers = np.arange(image_framenumbers[0],
                                 image_framenumbers[-1]+1,
                                 dtype=np.int64)

    assert N%2==1
    offset = N//2

    nan_im = np.ndarray(ims[0].shape, dtype=np.float)
    nan_im.fill(np.nan)

    results = []
    for center_fno in range( offset, len(all_framenumbers)-offset):
        center_fno += all_framenumbers[0]
        center_idx=np.searchsorted(image_framenumbers,center_fno,side='right')-1
        camn_pt_no = camn_pt_no_array[center_idx]
        ims_to_average = []
        coords_to_average = []
        for fno in range( center_fno-offset, center_fno+offset+1 ):
            idx = np.searchsorted(image_framenumbers,fno,side='right')-1
            if image_framenumbers[idx] == fno:
                ims_to_average.append( ims[idx] )
                coords_to_average.append( im_coords[idx] )
            ## else:
            ##     #print 'failed to find image %d'%fno
            ##     ims_to_average.append( nan_im )

        # XXX this is not very efficient.
        to_av = np.array(ims_to_average)
        ## print 'fno %d: min %.1f max %.1f'%(center_fno, to_av.min(), to_av.max())
        av_im = np.mean( to_av, axis=0 )

        n_images = len(ims_to_average)
        if n_images>=min_N:
            coords_to_average = np.array(coords_to_average)
            mean_lowerleft = np.mean( coords_to_average[:,:2], axis=0)
            results.append( (center_fno, av_im, n_images,
                             mean_lowerleft, camn_pt_no) )
    return results

def clip_and_math( raw_image, mean_image, xy, roiradius, maxsize ):
    roisize = 2*roiradius
    x,y=xy
    l = max(x-roiradius,0)
    b = max(y-roiradius,0)
    r = l+roisize
    t = b+roisize
    maxwidth,maxheight=maxsize
    if r > maxwidth:
        r = maxwidth
        l = r-roisize
    if t > maxheight:
        t = maxheight
        b = t-roisize

    raw_im = raw_image[b:t,l:r]
    mean_im = mean_image[b:t,l:r]
    absdiff_im = abs( mean_im - raw_im )

    return (l,b,r,t), raw_im, mean_im, absdiff_im


def doit(h5_filename=None,
         output_h5_filename=None,
         ufmf_filenames=None,
         kalman_filename=None,
         start=None,
         stop=None,
         view=None,
         ):
    """

    Copy all data in .h5 file (specified by h5_filename) to a new .h5
    file in which orientations are set based on image analysis of
    .ufmf files. Tracking data to associate 2D points from subsequent
    frames is read from the .h5 kalman file specified by
    kalman_filename.

    """
    if view is None:
        view = [ 'orig' for f in ufmf_filenames ]
    else:
        assert len(view)==len(ufmf_filenames)
    filename2view = dict(zip(ufmf_filenames,view))

    ca = core_analysis.get_global_CachingAnalyzer()
    obj_ids, use_obj_ids, is_mat_file, data_file, extra = ca.initial_file_load(
        kalman_filename)
    kalman_observations_2d_idxs = data_file.root.kalman_observations_2d_idxs[:]


    if os.path.exists( output_h5_filename ):
        raise RuntimeError(
            "will not overwrite old file '%s'"%output_h5_filename)
    output_h5 = tables.openFile( output_h5_filename, mode='w' )
    #with openFileSafe( output_h5_filename, mode='w') as output_h5:

    with openFileSafe( h5_filename, mode='r' ) as h5:

        for input_node in h5.root._f_iterNodes():
            # copy everything from source to dest
            input_node._f_copy(output_h5.root,recursive=True)
        print 'done copying'

        # Clear values in destination table that we may overwrite.
        dest_table = output_h5.root.data2d_distorted
        for colname in ['x','y','area','slope','eccentricity','cur_val',
                        'mean_val','sumsqf_val']:
            clear_col(dest_table,colname)
        dest_table.flush()
        print 'done clearing'

        camn2cam_id, cam_id2camns = result_utils.get_caminfo_dicts(h5)

        cam_id2fmfs = collections.defaultdict(list)
        for ufmf_filename in ufmf_filenames:
            fmf = ufmf.FlyMovieEmulator(ufmf_filename,
                                        #darken=-50,
                                        allow_no_such_frame_errors=True)
            timestamps = fmf.get_all_timestamps()

            cam_id = get_cam_id_from_filename(fmf.filename, cam_id2camns.keys())
            cam_id2fmfs[cam_id].append(
                (fmf,result_utils.Quick1DIndexer(timestamps)))

        # associate framenumbers with timestamps using 2d .h5 file
        data2d = h5.root.data2d_distorted[:] # load to RAM
        data2d_idxs = np.arange(len(data2d))
        h5_framenumbers = data2d['frame']
        h5_frame_qfi = result_utils.QuickFrameIndexer(h5_framenumbers)

        for obj_id_enum,obj_id in enumerate(use_obj_ids):
            print 'object %d of %d'%(obj_id_enum,len(use_obj_ids))

            # get all images for this camera and this obj_id

            obj_3d_rows = ca.load_dynamics_free_MLE_position( obj_id, data_file)

            this_obj_framenumbers = collections.defaultdict(list)
            this_obj_absdiff_images = collections.defaultdict(list)
            this_obj_im_coords = collections.defaultdict(list)
            this_obj_camn_pt_no = collections.defaultdict(list)

            for this_3d_row in obj_3d_rows:
                # iterate over each sample in the current camera
                framenumber = this_3d_row['frame']
                if start is not None:
                    if not framenumber >= start:
                        continue
                if stop is not None:
                    if not framenumber <= stop:
                        continue
                h5_2d_row_idxs = h5_frame_qfi.get_frame_idxs(framenumber)

                frame2d = data2d[h5_2d_row_idxs]
                frame2d_idxs = data2d_idxs[h5_2d_row_idxs]

                obs_2d_idx = this_3d_row['obs_2d_idx']
                kobs_2d_data = kalman_observations_2d_idxs[int(obs_2d_idx)]

                # Parse VLArray.
                this_camns = kobs_2d_data[0::2]
                this_camn_idxs = kobs_2d_data[1::2]

                # Now, for each camera viewing this object at this
                # frame, extract images.
                for camn, camn_pt_no in zip(this_camns, this_camn_idxs):

                    # find 2D point corresponding to object
                    cam_id = camn2cam_id[camn]

                    movie_tups_for_this_camn = cam_id2fmfs[cam_id]
                    cond = ((frame2d['camn']==camn) &
                            (frame2d['frame_pt_idx']==camn_pt_no))
                    idxs = np.nonzero(cond)[0]
                    assert len(idxs)==1
                    idx = idxs[0]

                    orig_data2d_rownum = frame2d_idxs[idx]
                    frame_timestamp = frame2d[idx]['timestamp']
                    found = None
                    for fmf, fmf_timestamp_qi in movie_tups_for_this_camn:
                        fmf_fnos = fmf_timestamp_qi.get_idxs(frame_timestamp)
                        if not len(fmf_fnos):
                            continue
                        assert len(fmf_fnos)==1

                        # should only be one .ufmf with this frame and cam_id
                        assert found is None

                        fmf_fno = fmf_fnos[0]
                        found = (fmf, fmf_fno )
                    if found is None:
                        ## print 'no image data for frame timestamp %s cam_id %s'%(
                        ##     repr(frame_timestamp),cam_id)
                        continue
                    fmf, fmf_fno = found
                    image, fmf_timestamp = fmf.get_frame( fmf_fno )
                    mean_image = fmf._ufmf.get_mean_for_timestamp(fmf_timestamp)

                    xy = (int(round(frame2d[idx]['x'])),
                          int(round(frame2d[idx]['y'])))
                    maxsize = (fmf.get_width(), fmf.get_height())

                    # Accumulate cropped images. Note that the region
                    # of the full image that the cropped image
                    # occupies changes over time as the tracked object
                    # moves. Thus, averaging these cropped-and-shifted
                    # images is not the same as simply averaging the
                    # full frame.

                    roiradius = fmf._ufmf._image_radius # Naughty
                    tmp = clip_and_math( image, mean_image, xy, roiradius,
                                         maxsize )
                    im_coords, raw_im, mean_im, absdiff_im = tmp

                    this_obj_framenumbers[camn].append( framenumber )
                    this_obj_absdiff_images[camn].append(absdiff_im)
                    this_obj_im_coords[camn].append(im_coords)
                    this_obj_camn_pt_no[camn].append(orig_data2d_rownum)
                    if 0:
                        fname = 'obj%05d_%s_frame%07d_pt%02d.png'%(
                            obj_id,cam_id,framenumber,camn_pt_no)
                        plot_image_subregion( raw_im, mean_im, absdiff_im,
                                              roiradius, fname, im_coords,
                                              view=filename2view[fmf.filename],
                                              )

            # Now, all the frames from all cameras for this obj_id
            # have been gathered. Do a camera-by-camera analysis.
            fpc = realtime_image_analysis.FitParamsClass()

            for camn in this_obj_absdiff_images:
                cam_id = camn2cam_id[camn]
                image_framenumbers = np.array(this_obj_framenumbers[camn])
                absdiff_images = this_obj_absdiff_images[camn]
                im_coords = this_obj_im_coords[camn]
                camn_pt_no_array = this_obj_camn_pt_no[camn]

                all_framenumbers = np.arange(image_framenumbers[0],
                                             image_framenumbers[-1]+1)

                results = running_average( image_framenumbers,
                                           absdiff_images,
                                           im_coords,
                                           camn_pt_no_array,
                                           )

                # The variable fno (the first element of the results
                # tuple) is guaranteed to be contiguous and to span
                # the range from the first to last frames available.

                for (fno, av_im, n_images, lowerleft,
                     orig_data2d_rownum) in results:

                    # Clip image to reduce moment arms.
                    thresh = 7 # arbitrary
                    av_im[av_im <= thresh] = 0

                    # XXX todo: connected components labels?

                    fast_av_im = FastImage.asfastimage( av_im.astype(np.uint8) )
                    try:
                        (x0_roi, y0_roi, area, slope, eccentricity) = fpc.fit(
                            fast_av_im )
                    except realtime_image_analysis.FitParamsError, err:
                        x0_roi = np.nan
                        y0_roi = np.nan
                        area, slope, eccentricity = np.nan, np.nan, np.nan

                    x0 = x0_roi + lowerleft[0]
                    y0 = y0_roi + lowerleft[1]

                    if 1:
                        for row in dest_table.iterrows(
                            start=orig_data2d_rownum,
                            stop=orig_data2d_rownum+1):

                            row['x']=x0
                            row['y']=y0
                            row['area']=area
                            row['slope']=slope
                            row['eccentricity']=eccentricity
                            row.update() # save data

                    if 0:
                        # Display debugging images
                        fname = 'av_obj%05d_%s_frame%07d.png'%(
                            obj_id,cam_id,fno)


                        if 1:
                            # increase contrast
                            scale = 30.0
                            av_im = np.clip(av_im*scale,0,255)

                        margin = 10
                        scale = 5

                        yintercept = y0-slope*x0
                        xplt=np.array([lowerleft[0]-5,
                                       lowerleft[0]+av_im.shape[1]+5])
                        yplt=slope*xplt+yintercept
                        canv=benu.Canvas(fname,
                                         scale*av_im.shape[1] + 2*margin,
                                         scale*av_im.shape[0] + 2*margin)
                        display_rect = (margin,margin,
                                        scale*av_im.shape[1],
                                        scale*av_im.shape[0])
                        user_rect = (lowerleft[0],lowerleft[1],
                                     av_im.shape[1], av_im.shape[0])
                        with canv.set_user_coords(display_rect, user_rect):
                            canv.imshow(av_im,lowerleft[0],lowerleft[1])
                            canv.plot(xplt,yplt,color_rgba=(0,1,0,.5))
                        if 0:
                            canv.text( 'x,y=(%.1f,%.1f) slope=%.1f'%(x0,y0,slope),
                                       0,12)
                            canv.text( 'xplt %s'%str(xplt),  0,24)
                            canv.text( 'yplt %s'%str(yplt),  0,36)
                            canv.text( 'lowerleft %s'%str(lowerleft),  0,48)
                        canv.save()

            # Save results to new table
            if 0:
                recarray = np.rec.array(
                    list_of_rows_of_data2d,
                    dtype=Info2DCol_description)
                dest_table.append( recarray )
                dest_table.flush()

    data_file.close()
    output_h5.close()

def main():
    usage = '%prog [options]'

    parser = OptionParser(usage)

    parser.add_option("--ufmfs", type='string',
                      help=("sequence of .ufmf filenames "
                            "(e.g. 'cam1.ufmf:cam2.ufmf')"))

    parser.add_option("--view", type='string',
                      help="how to view .ufmf files")

    parser.add_option("--h5", type='string',
                      help=".h5 file with data2d_distorted (REQUIRED)")

    parser.add_option("--output-h5", type='string',
                      help="filename for output .h5 file with data2d_distorted")

    parser.add_option("--kalman", dest="kalman_filename", type='string',
                      help=".h5 file with kalman data and 3D reconstructor")

    parser.add_option("--start", type='int', default=None,
                      help="frame number to begin analysis on")

    parser.add_option("--stop", type='int', default=None,
                      help="frame number to end analysis on")

    (options, args) = parser.parse_args()

    if options.ufmfs is None:
        raise ValueError('--ufmfs option must be specified')

    if options.h5 is None:
        raise ValueError('--h5 option must be specified')

    if options.output_h5 is None:
        raise ValueError('--output-h5 option must be specified')

    if options.kalman_filename is None:
        raise ValueError('--kalman option must be specified')

    ufmf_filenames = options.ufmfs.split(os.pathsep)
    ## print 'ufmf_filenames',ufmf_filenames
    ## print 'options.h5',options.h5

    if options.view is not None:
        view = eval(options.view)

    doit(ufmf_filenames=ufmf_filenames,
         h5_filename=options.h5,
         kalman_filename=options.kalman_filename,
         start=options.start,
         stop=options.stop,
         view=view,
         output_h5_filename=options.output_h5,
         )

if __name__=='__main__':
    main()