#!/usr/bin/env python3
"""Print a compact summary of one or more harness run directories (result.json): codec negotiation, browser getStats, Rust-side counters, ICE states."""
import json,sys,glob
for d in sys.argv[1:]:
    r=json.load(open(d+'/result.json'))
    print('###',d.split('/')[-1], 'wall',r['wall_s'],'done',r['page_done'],'fatal',r['page_fatal'])
    cs=r.get('codec_summary',{})
    for k,v in cs.items(): print('  codec',k,v)
    for s in r['sessions']:
        i=s.get('in') or {}; o=s.get('out') or {}
        print('  browser sid',s['sid'],'cs',s.get('final_cs'),'in rx',i.get('packetsReceived'),'lost',i.get('packetsLost'),'jit',i.get('jitter'),'lvl',round(s.get('max_audioLevel') or 0,3),'codec',s.get('codecs_in'),'out tx',o.get('packetsSent'),'stall',s.get('max_stall_ms'),'domHz',s.get('dom_hz_browser'),'pair',s.get('pair') and (s['pair']['local'],s['pair']['remote']))
        print('   states',[(x[1]) for x in s.get('states',[])][:14])
    for a in r['answerer_reports']:
        print('  rust',a['id'],'rx',a['rx'],'lost',a['lost'],'pt',a['pt_counts'],'tx',a['tx'],'maxgap',a['max_gap_ms'],'spec',a['spectrum']['dominant_hz_top'],'sil',a['spectrum']['silent_blocks'],'cands',a['remote_cands'])
    print('  rust states',[(x[0]-r.get('kill_t_ms',x[0]) if r.get('kill_t_ms') else x[0]%100000, x[1],x[2],x[3]) for x in r['answerer_states']])
    if r.get('kill_t_ms'): print('  DISCONNECT after ms',r.get('disconnect_detected_after_ms'), 'args', r['args']['answerer_arg'])
    for e in r['answerer_events']:
        if e['ev'] in('offer-accepted',): print('  offer-accepted',{k:v for k,v in e.items() if k!='answer_sdp'})
        if e['ev'] in('offer-rejected','offer-PANIC-CAUGHT','SESSION_PANIC'): print('  !!',e)
