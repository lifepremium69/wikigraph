import sys
import re
import json
import time
import requests
from collections import deque
from bs4 import BeautifulSoup
from flask import Flask, Response, render_template_string, request, stream_with_context

# ==========================================
# FLASK APPLICATION SETUP
# ==========================================
app = Flask(__name__)

# ==========================================
# GRAPH LOGIC (Refactored for Web)
# ==========================================

HEADERS = {
    'User-Agent': 'CompanyKnowledgeGraphExplorer/1.0 (https://example.com/contact; your.email@example.com)'
}

WIKI_URL = "https://en.wikipedia.org/wiki/"

class GraphSearcher:
    def __init__(self):
        self.graph_nodes = []
        self.graph_edges = []
        self.visited = set()
        self.queue = deque()
        self.node_id_counter = 0
        self.node_cache = {}  # (label, type) -> node_id
        self.edge_cache = set()

    def get_next_node_id(self) -> int:
        cur = self.node_id_counter
        self.node_id_counter += 1
        return cur

    def get_or_create_node(self, label: str, node_type: str = "company", weight: float = 1.0) -> int:
        key = (label, node_type)
        if key in self.node_cache:
            return self.node_cache[key]

        nid = self.get_next_node_id()
        self.graph_nodes.append({
            'data': {
                'id': str(nid), # Cytoscape IDs should be strings
                'label': label,
                'weight': weight
            },
            'classes': node_type
        })
        self.node_cache[key] = nid
        return nid

    def get_node_id(self, label: str) -> int or None:
        # Check cache first for speed
        for key, nid in self.node_cache.items():
            if key[0] == label:
                return nid
        return None

    def add_edge(self, source_label: str, target_label: str, relation: str):
        source_id = self.get_node_id(source_label)
        target_id = self.get_node_id(target_label)

        if source_id is None or target_id is None:
            return

        # Normalize edge for de-duplication if strictly undirected, 
        # but here we keep direction for OWNS/PARENT_OF
        if source_id > target_id and relation not in ["OWNS", "KEY_PERSON_OF", "PARENT_OF"]:
            src, tgt = target_id, source_id
        else:
            src, tgt = source_id, target_id

        edge_key = (src, tgt, relation)
        if edge_key in self.edge_cache:
            return

        self.graph_edges.append({
            'data': {
                'id': f"e{len(self.graph_edges)}",
                'source': str(source_id),
                'target': str(target_id),
                'label': relation
            }
        })
        self.edge_cache.add(edge_key)

    def find_wiki_page(self, title: str) -> str or None:
        page_url = WIKI_URL + title.replace(' ', '_')
        try:
            response = requests.get(page_url, headers=HEADERS, timeout=5)
            if response.status_code == 404:
                return None
            if response.history:
                return response.url.split('/')[-1].replace('_', ' ')
            return title
        except:
            return None

    def get_infobox_data(self, company_title: str) -> dict:
        page_url = WIKI_URL + company_title.replace(' ', '_')
        try:
            response = requests.get(page_url, headers=HEADERS, timeout=5)
            response.raise_for_status()
        except:
            return {}

        soup = BeautifulSoup(response.content, 'html.parser')
        infobox = soup.find('table', class_='infobox')
        if not infobox:
            return {}

        data = {}
        fields_to_scrape = {
            "parent": "PARENT_OF",
            "subsidiaries": "OWNS",
            "founder": "FOUNDED",
            "key people": "KEY_PERSON_OF",
            "owner": "PARENT_OF"
        }

        for th_tag in infobox.find_all('th'):
            th_text = th_tag.get_text().strip().lower()
            for field, relation in fields_to_scrape.items():
                if field in th_text:
                    td_tag = th_tag.find_next_sibling('td')
                    if td_tag:
                        links = td_tag.find_all('a', href=True)
                        if relation not in data:
                            data[relation] = []
                        for link in links:
                            if '#' in link['href']: continue
                            title = link.get('title')
                            if title and not title.startswith('File:'):
                                clean_title = re.sub(r'\s*\([^)]*\)$', '', title)
                                data[relation].append(clean_title)
        return data

    def run_search_generator(self, root_company, max_depth):
        """Generator that yields progress updates and finally the result."""
        
        yield json.dumps({"status": "log", "message": f"Verifying page for '{root_company}'..."}) + "\n"
        
        root_title = self.find_wiki_page(root_company)
        if not root_title:
            yield json.dumps({"status": "error", "message": f"Wikipedia page not found for '{root_company}'"}) + "\n"
            return

        self.get_or_create_node(root_title, 'company', 2.0)
        self.queue.append((root_title, None, 0))

        processed = 0
        limit = 300 # Safety limit

        while self.queue and processed < limit:
            cur, parent, depth = self.queue.popleft()
            
            # Progress update
            percent = int((processed / limit) * 100)
            yield json.dumps({
                "status": "progress", 
                "percent": percent, 
                "message": f"[{depth}/{max_depth}] Fetching: {cur}..."
            }) + "\n"

            if depth > max_depth:
                continue

            if cur in self.visited:
                continue
            self.visited.add(cur)

            # Link to parent if exists
            if parent:
                self.add_edge(parent, cur, "OWNS") # Simplification for visualization flow

            infobox_data = self.get_infobox_data(cur)
            
            for relation_type, related_titles in infobox_data.items():
                for related_title in related_titles:
                    if relation_type in ["FOUNDED", "KEY_PERSON_OF"]:
                        node_type = 'person'
                        related_title = related_title.split('(')[0].strip()
                    else:
                        node_type = 'company'

                    self.get_or_create_node(related_title, node_type, 1.0)

                    # Add Edge Logic
                    if relation_type in ["KEY_PERSON_OF", "FOUNDED"]:
                        self.add_edge(related_title, cur, relation_type)
                    elif relation_type == "PARENT_OF":
                        self.add_edge(related_title, cur, "OWNS")
                    elif relation_type == "OWNS":
                        self.add_edge(cur, related_title, "OWNS")

                    # Enqueue
                    if node_type == 'company' and related_title not in self.visited:
                        # Simple check to avoid circular queuing if already in queue (not perfect but helpful)
                        if depth + 1 <= max_depth:
                            if relation_type in ["PARENT_OF", "OWNER"]:
                                self.queue.appendleft((related_title, cur, depth + 1))
                            else:
                                self.queue.append((related_title, cur, depth + 1))
            
            processed += 1
            # Small delay to be polite to Wikipedia and allow UI to update
            time.sleep(0.1) 

        # Final Weight Calculation
        for node in self.graph_nodes:
            nid = node['data']['id']
            deg = sum(1 for e in self.graph_edges if e['data']['source'] == nid or e['data']['target'] == nid)
            node['data']['weight'] = max(2.0 if node['classes'] == 'company' else 1.0, deg * 0.5)

        # Final Result
        result = {
            "status": "complete",
            "percent": 100,
            "message": "Graph generation complete!",
            "data": {
                "nodes": self.graph_nodes,
                "edges": self.graph_edges
            }
        }
        yield json.dumps(result) + "\n"

# ==========================================
# FRONTEND TEMPLATE
# ==========================================

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WikiGraph Explorer</title>
    
    <!-- Tailwind CSS -->
    <script src="https://cdn.tailwindcss.com"></script>
    
    <!-- CORE Cytoscape -->
    <script src="https://unpkg.com/cytoscape@3.26.0/dist/cytoscape.min.js"></script>

    <!-- DAGRE LAYOUT DEPENDENCIES -->
    <script src="https://unpkg.com/dagre@0.8.5/dist/dagre.min.js"></script>
    <script src="https://unpkg.com/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>

    <!-- FCOSE LAYOUT DEPENDENCIES -->
    <script src="https://unpkg.com/layout-base@1.0.1/layout-base.js"></script>
    <script src="https://unpkg.com/cose-base@1.0.1/cose-base.js"></script>
    <script src="https://unpkg.com/cytoscape-fcose@2.2.0/cytoscape-fcose.js"></script>

    <!-- KLAY LAYOUT DEPENDENCIES -->
    <script src="https://unpkg.com/klayjs@0.4.1/klay.js"></script>
    <script src="https://unpkg.com/cytoscape-klay@3.1.4/cytoscape-klay.js"></script>
    
    <!-- Utilities -->
    <script src="https://unpkg.com/cytoscape-svg@0.4.0/cytoscape-svg.js"></script>

    <style>
        body, html { height: 100%; overflow: hidden; }
        #cy { width: 100%; height: 100%; background-color: #f8fafc; }
        .glass-panel {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(226, 232, 240, 0.8);
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }
    </style>
</head>
<body class="font-sans text-slate-800">

    <!-- SIDEBAR CONTROLS -->
    <div class="absolute top-4 left-4 z-50 w-80 flex flex-col gap-4">
        
        <!-- SEARCH BOX -->
        <div class="glass-panel p-5 rounded-xl">
            <h1 class="text-xl font-bold mb-4 text-blue-600 flex items-center gap-2">
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
                WikiGraph
            </h1>
            
            <div class="space-y-3">
                <div>
                    <label class="text-xs font-semibold uppercase text-slate-500">Company Name</label>
                    <input type="text" id="companyInput" placeholder="e.g. Nvidia" class="w-full mt-1 px-3 py-2 bg-white border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500">
                </div>
                
                <div>
                    <label class="text-xs font-semibold uppercase text-slate-500">Search Depth</label>
                    <select id="depthInput" class="w-full mt-1 px-3 py-2 bg-white border border-slate-300 rounded-lg">
                        <option value="1">1 (Direct connections)</option>
                        <option value="2" selected>2 (Standard)</option>
                        <option value="3">3 (Deep - Slower)</option>
                    </select>
                </div>

                <button onclick="startSearch()" id="runBtn" class="w-full py-2 bg-blue-600 hover:bg-blue-700 text-white font-bold rounded-lg transition-colors flex justify-center items-center gap-2">
                    Run Analysis
                </button>
            </div>
        </div>

        <!-- PROGRESS BAR (Hidden by default) -->
        <div id="progressPanel" class="hidden glass-panel p-4 rounded-xl">
            <div class="flex justify-between text-xs font-semibold mb-1">
                <span id="progressStatus">Initializing...</span>
                <span id="progressPercent">0%</span>
            </div>
            <div class="w-full bg-slate-200 rounded-full h-2.5 overflow-hidden">
                <div id="progressBar" class="bg-blue-600 h-2.5 rounded-full transition-all duration-300" style="width: 0%"></div>
            </div>
            <p id="progressDetail" class="text-xs text-slate-500 mt-2 truncate">Starting up...</p>
        </div>

        <!-- GRAPH CONTROLS (Hidden until complete) -->
        <div id="graphControls" class="hidden glass-panel p-4 rounded-xl space-y-3">
            <div>
                <label class="text-xs font-semibold uppercase text-slate-500">Layout</label>
                <select id="layoutSelect" onchange="applyLayout(this.value)" class="w-full mt-1 px-3 py-2 bg-white border border-slate-300 rounded-lg text-sm">
                    <option value="klay" selected>Klay (Default)</option>
                    <option value="fcose">fCoSE (Physics)</option>
                    <option value="dagre">Dagre (Hierarchical)</option>
                    <option value="grid">Grid</option>
                    <option value="concentric">Concentric</option>
                    <option value="circle">Circle</option>
                </select>
            </div>
            
            <div class="grid grid-cols-2 gap-2">
                <button onclick="saveGraph('png')" class="py-2 px-3 bg-white border border-slate-300 hover:bg-slate-50 rounded-lg text-xs font-bold text-slate-700">
                    Save PNG
                </button>
                <button onclick="saveGraph('jpg')" class="py-2 px-3 bg-white border border-slate-300 hover:bg-slate-50 rounded-lg text-xs font-bold text-slate-700">
                    Save JPG
                </button>
            </div>
             <button onclick="fitGraph()" class="w-full py-2 px-3 bg-slate-100 border border-slate-300 hover:bg-slate-200 rounded-lg text-xs font-bold text-slate-700">
                Re-Center Graph
            </button>
        </div>
    </div>

    <!-- MAIN CANVAS -->
    <div id="cy"></div>

    <script>
        var cy;
        const ZOOM_INCREMENT = 0.05;

        // Ensure global access for auto-registration logic of extensions
        if (typeof dagre !== 'undefined') window.dagre = dagre;

        document.addEventListener('DOMContentLoaded', function() {
            initEmptyGraph();
            setupCustomZoom(); 
        });
        
        function setupCustomZoom() {
            const container = document.getElementById('cy');
            
            // Disable Cytoscape's default zoom behavior
            cy.userZoomingEnabled(false);

            container.addEventListener('wheel', function(event) {
                event.preventDefault(); // Stop default page scroll/Cytoscape scroll

                if (!cy) return;
                
                const factor = event.deltaY < 0 ? 1 + ZOOM_INCREMENT : 1 - ZOOM_INCREMENT;
                const newZoom = cy.zoom() * factor;

                // Determine the position for zooming (use cursor position for focus)
                const containerRect = container.getBoundingClientRect();
                const panX = event.clientX - containerRect.left;
                const panY = event.clientY - containerRect.top;

                // Zoom towards the cursor position
                cy.zoom({
                    level: newZoom,
                    renderedPosition: { x: panX, y: panY }
                });
            }, { passive: false });
        }


        function initEmptyGraph() {
            cy = cytoscape({
                container: document.getElementById('cy'),
                style: [
                    { selector: 'node', style: {
                        'label': 'data(label)', 'text-valign': 'center', 'color': '#fff',
                        'text-outline-width': 2, 'text-outline-color': '#1e293b',
                        'background-color': '#3b82f6', 'shape': 'roundrectangle',
                        'width': 'mapData(weight, 1, 6, 40, 100)', 'height': 'mapData(weight, 1, 6, 40, 100)',
                        'font-size': '12px'
                    }},
                    { selector: '.person', style: {
                        'background-color': '#f97316', 'shape': 'ellipse'
                    }},
                    { selector: 'edge', style: {
                        'width': 2, 'line-color': '#cbd5e1', 'target-arrow-color': '#cbd5e1',
                        'target-arrow-shape': 'triangle', 'curve-style': 'bezier',
                        'label': 'data(label)', 'font-size': '9px', 'text-rotation': 'autorotate',
                        'text-background-color': '#ffffff', 'text-background-opacity': 0.8
                    }},
                    { selector: 'edge[label="OWNS"]', style: { 'line-color': '#22c55e', 'target-arrow-color': '#22c55e' }},
                    { selector: 'edge[label="KEY_PERSON_OF"]', style: { 'line-color': '#ef4444', 'target-arrow-color': '#ef4444' }}
                ]
            });
        }

        async function startSearch() {
            const company = document.getElementById('companyInput').value;
            const depth = document.getElementById('depthInput').value;

            if (!company) { 
                const button = document.getElementById('runBtn');
                button.textContent = "Enter Company!";
                button.classList.remove('bg-blue-600');
                button.classList.add('bg-red-500');
                setTimeout(() => {
                    button.textContent = "Run Analysis";
                    button.classList.remove('bg-red-500');
                    button.classList.add('bg-blue-600');
                }, 2000);
                return; 
            }

            // UI Updates
            document.getElementById('runBtn').disabled = true;
            document.getElementById('runBtn').classList.add('opacity-50');
            document.getElementById('progressPanel').classList.remove('hidden');
            document.getElementById('graphControls').classList.add('hidden');
            
            // Reset Progress
            updateProgress(0, "Connecting to server...");

            // Start Stream
            const response = await fetch(`/search?company=${encodeURIComponent(company)}&depth=${depth}`);
            const reader = response.body.getReader();
            const decoder = new TextDecoder();

            let buffer = '';

            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                
                // Process complete JSON lines
                buffer = lines.pop(); 

                for (const line of lines) {
                    if (!line.trim()) continue;
                    try {
                        const msg = JSON.parse(line);
                        handleServerMessage(msg);
                    } catch (e) {
                        console.error("Parse error", e);
                    }
                }
            }

            document.getElementById('runBtn').disabled = false;
            document.getElementById('runBtn').classList.remove('opacity-50');
        }

        function handleServerMessage(msg) {
            if (msg.status === 'progress') {
                updateProgress(msg.percent, msg.message);
            } else if (msg.status === 'log') {
                document.getElementById('progressDetail').innerText = msg.message;
            } else if (msg.status === 'complete') {
                updateProgress(100, "Done!");
                renderGraph(msg.data);
                setTimeout(() => {
                    document.getElementById('progressPanel').classList.add('hidden');
                    document.getElementById('graphControls').classList.remove('hidden');
                }, 1000);
            } else if (msg.status === 'error') {
                document.getElementById('progressDetail').innerText = `Error: ${msg.message}`;
                document.getElementById('progressPanel').classList.remove('bg-blue-600');
                document.getElementById('progressPanel').classList.add('bg-red-500');
                
                document.getElementById('runBtn').disabled = false;
                document.getElementById('runBtn').classList.remove('opacity-50');
            }
        }

        function updateProgress(percent, text) {
            document.getElementById('progressBar').style.width = percent + '%';
            document.getElementById('progressPercent').innerText = percent + '%';
            if (text) document.getElementById('progressDetail').innerText = text;
        }

        function renderGraph(data) {
            cy.elements().remove();
            cy.add(data.nodes);
            cy.add(data.edges);
            // DEFAULT LAYOUT IS NOW KLAY
            applyLayout('klay');
        }

        function applyLayout(name) {
            let options = { 
                name: name, 
                animate: true, 
                animationDuration: 800,
                padding: 50
            };
            
            if (name === 'fcose') { options.quality = 'proof'; options.nodeDimensionsIncludeLabels = true; }
            if (name === 'dagre' || name === 'klay') { options.rankDir = 'TB'; options.nodeSep = 50; options.rankSep = 100; }
            
            try {
                cy.layout(options).run();
            } catch (err) {
                console.error("Layout failed:", err);
                alert("Failed to load layout: " + name + ". Check console.");
            }
        }

        function saveGraph(type) {
            let blob;
            if (type === 'png') blob = cy.png({ output: 'blob', bg: 'white', full: true });
            if (type === 'jpg') blob = cy.jpg({ output: 'blob', bg: 'white', full: true });
            
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = `graph.${type}`;
            link.click();
        }

        function fitGraph() {
            cy.fit();
        }
    </script>
</body>
</html>
"""

# ==========================================
# FLASK ROUTES
# ==========================================

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route('/search')
def search():
    company = request.args.get('company', '')
    try:
        depth = int(request.args.get('depth', 2))
    except:
        depth = 2

    if not company:
        return Response("Missing company name", status=400)

    searcher = GraphSearcher()
    
    # Return a streaming response
    return Response(
        stream_with_context(searcher.run_search_generator(company, depth)),
        mimetype='application/json'
    )

# ==========================================
# MAIN ENTRY POINT
# ==========================================
if __name__ == '__main__':
    print("Starting WikiGraph Explorer...")
    print("Go to http://127.0.0.1:5000 in your browser.")
    app.run(debug=True, port=5000)
