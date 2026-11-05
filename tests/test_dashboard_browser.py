"""Opt-in real DOM regressions: set BRIDGE_BROWSER to a Chromium executable."""
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import test_dashboard
from plex_bridge.dashboard import SCRIPT


@unittest.skipUnless(os.environ.get('BRIDGE_BROWSER'), 'Set BRIDGE_BROWSER for real browser checks')
class DashboardBrowserTests(unittest.TestCase):
    def run_browser(self, scenario):
        fixture=test_dashboard.DashboardTests();fixture.setUp()
        mock='''<script>
window.setInterval=()=>0; window.confirm=()=>true; window.requests=[];
window.fetch=async(url,options={})=>{
 if(options.method==='POST'){
  requests.push({url:String(url),body:String(options.body)});
  if(String(url)!=='/dashboard/action')return new Response('not found',{status:404});
  if(SCENARIO==='http-error')return new Response('rejected',{status:403});
  if(SCENARIO==='network-error')throw new TypeError('Network unavailable');
  return new Response(JSON.stringify({message:'Synthetic action accepted'}),{headers:{'Content-Type':'application/json'}});
 }
 return new Response(document.documentElement.outerHTML,{headers:{'Content-Type':'text/html'}});
};
</script>'''.replace('SCENARIO',json.dumps(scenario))
        check='''<script>
(async()=>{
 const cases=[...document.querySelectorAll('.room-action')].filter(f=>!f.querySelector('button').disabled).map(f=>({room:f.closest('.room-card').dataset.room,action:f.querySelector('[name="action"]').value}));
 const outputs=[];
 for(const item of cases){
  const card=[...document.querySelectorAll('.room-card')].find(c=>c.dataset.room===item.room);
  const form=[...card.querySelectorAll('.room-action')].find(f=>f.querySelector('[name="action"]').value===item.action);
  form.requestSubmit(form.querySelector('button'));
  await new Promise(resolve=>setTimeout(resolve,50));
  const updated=[...document.querySelectorAll('.room-card')].find(c=>c.dataset.room===item.room);
  outputs.push(updated.querySelector('.room-result').textContent);
 }
 const output=document.createElement('pre');output.id='browser-result';
 output.textContent=JSON.stringify({cases,requests,outputs});document.body.append(output);
})();
</script>'''
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);page=root/'fixture.html'
            page.write_text('<!doctype html><html><head><meta charset="utf-8"></head><body>'+fixture.ui.page().replace(SCRIPT,mock+SCRIPT)+check+'</body></html>')
            result=subprocess.run([os.environ['BRIDGE_BROWSER'],'--headless','--disable-gpu','--disable-background-networking','--disable-component-update','--no-first-run','--user-data-dir='+str(root/'profile'),'--virtual-time-budget=5000','--dump-dom',page.as_uri()],capture_output=True,text=True,timeout=40)
            self.assertEqual(result.returncode,0,result.stderr[-1000:])
            found=re.search(r'<pre id="browser-result">(.*?)</pre>',result.stdout)
            self.assertIsNotNone(found,'Browser did not finish the fixture')
            return json.loads(html.unescape(found.group(1)))

    def test_every_enabled_control_posts_to_form_attribute_despite_action_input(self):
        result=self.run_browser('success')
        self.assertGreaterEqual(len(result['cases']),10)
        self.assertEqual(len(result['requests']),len(result['cases']))
        for case,request in zip(result['cases'],result['requests']):
            self.assertEqual(request['url'],'/dashboard/action')
            self.assertIn('action='+case['action'],request['body'])
            self.assertIn('requester='+case['room'],request['body'])
        self.assertTrue(all(message=='Synthetic action accepted' for message in result['outputs']))

    def test_http_rejection_is_not_reported_as_connection_interruption(self):
        result=self.run_browser('http-error')
        self.assertTrue(all('HTTP 403' in message for message in result['outputs']))
        self.assertEqual(len(result['requests']),len(result['cases']))

    def test_network_failure_is_not_retried(self):
        result=self.run_browser('network-error')
        self.assertTrue(all('Connection interrupted' in message for message in result['outputs']))
        self.assertEqual(len(result['requests']),len(result['cases']))
