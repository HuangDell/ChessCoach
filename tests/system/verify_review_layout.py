"""Offline browser regression for review layout; no Engine, model, or personal data."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    root = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(root / 'frontend')))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for width, height in [(1920, 1080), (1440, 900), (1280, 720), (390, 844)]:
                    page = browser.new_page(viewport={'width': width, 'height': height})
                    errors = []
                    requests = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    def mock(route):
                        requests.append(route.request.url)
                        route.fulfill(json={'empty': True, 'enabled': False, 'online': True, 'games': [], 'agent': {'available': False}})
                    page.route('**/api/**', mock)
                    page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                    page.locator('#firstrun').evaluate('(el) => el.hidden = true')
                    assert page.locator('#analysis-engine').is_visible()
                    assert not page.locator('#analysis-coach').is_visible()
                    assert not page.locator('#history-col').is_visible()
                    page.locator('#history-toggle').click()
                    assert page.locator('#history-col').is_visible()
                    page.keyboard.press('Escape')
                    assert page.locator('#history-col').evaluate('(el) => el.inert')
                    assert page.locator('#history-toggle').evaluate('(el) => el === document.activeElement')
                    # Exercise rendering modules against the real DOM with deterministic fixture facts.
                    page.evaluate('''async () => {
                      const $ = id => document.getElementById(id);
                      const {createWorkspaceView} = await import('/modules/review/workspace-view.js');
                      const {createReviewGraph} = await import('/modules/review/graph.js');
                      const critical = {critical_id:'p1', ply:1, move_san:'e4', classification:'mistake', win_loss:10,
                        facts:{}, played_line:{san:['e4'],uci:['e2e4']}, best_line:{san:['d4'],uci:['d2d4']}};
                      const snapshot = {timeline:[{node:0,win_white:50},{node:1,win_white:40}], cur:0, orient:'white',
                        criticalPositions:[critical], activeCriticalId:'p1', reviewedMoveNode:0,
                        engineReview:{moves:[{ply:1,move_san:'e4',classification:'good',best_move:{san:'d4'}}]}};
                      const view = createWorkspaceView({$,getSnapshot:()=>snapshot,
                        setWorkflowState:(...args)=>view.renderWorkflow(...args),onSelectCritical(){},onSelectEngineMove(){},wireVariationLinks(){}});
                      window.fixture = {view,snapshot,critical};
                      createReviewGraph({$,getSnapshot:()=>snapshot,onGotoNode(){},onSelectCritical(){},onSelectMistake(){}}).render();
                      view.renderCritical(critical);
                      $('review-position-list').innerHTML = '<button>Position</button>'.repeat(60);
                    }''')
                    graph = page.locator('#graph-wrap').bounding_box()
                    assert round(graph['height']) == 72, graph
                    if width > 900:
                        assert graph['y'] + graph['height'] < height, graph
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    ids = page.locator('[id]').evaluate_all('(els) => els.map(el => el.id)')
                    assert len(ids) == len(set(ids))
                    count = len(requests)
                    page.locator('#analysis-tab-coach').click()
                    page.locator('#chat-input').fill('keep my draft')
                    page.locator('#analysis-tab-engine').click()
                    page.locator('#analysis-tab-engine').press('ArrowRight')
                    assert page.locator('#analysis-tab-coach').get_attribute('aria-selected') == 'true'
                    page.evaluate('fixture.view.renderCursor()')
                    assert page.locator('#analysis-coach').is_visible()
                    assert page.locator('#chat-input').input_value() == 'keep my draft'
                    assert not page.locator('#ai-explanation-action').is_visible()
                    page.evaluate("document.body.classList.add('retry-mode'); document.getElementById('retry-panel').hidden = false")
                    assert page.locator('#retry-panel').is_visible()
                    assert not page.locator('#analysis-tabs').is_visible()
                    page.evaluate("document.body.classList.remove('retry-mode'); document.getElementById('retry-panel').hidden = true")
                    assert page.locator('#analysis-coach').is_visible()
                    page.evaluate("fixture.view.renderFreeAnalysis('1. e4', 1, {moveSan:'e4', verdict:'pending'})")
                    page.locator('#analysis-tab-engine').click()
                    assert page.locator('#workflow-analysis').is_visible()
                    assert not page.locator('#engine-position-content').is_visible()
                    # Imported-game reset clears both analysis sources, while preserving the chosen tab.
                    page.evaluate("fixture.view.renderWorkflow('analyzing_scan', 'Scanning game', 'Working', '')")
                    assert not page.locator('#ai-explanation-action').is_visible()
                    assert not page.locator('#engine-position-content').is_visible()
                    page.evaluate("document.body.classList.add('puzzle-mode'); document.getElementById('puzzle-rail').hidden = false")
                    assert not page.locator('.navigation-col').is_visible()
                    assert not page.locator('.side-col').is_visible()
                    assert page.locator('#puzzle-rail').is_visible()
                    assert len(requests) == count, 'Presentation changes must not request models or API data'
                    assert not errors, errors
                    page.close()
                    print(f'{width}×{height}: passed')
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
