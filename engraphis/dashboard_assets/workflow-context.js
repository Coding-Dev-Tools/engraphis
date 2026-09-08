/* Memory-workflow preferences and connection checklist. No memory content is stored here. */
(() => {
  'use strict';

  const clean = value => typeof value === 'string' ? value.trim().slice(0, 200) : '';
  const title = memory => {
    const explicit = memory && typeof memory.title === 'string' ? memory.title.trim() : '';
    if (explicit) return explicit;
    const content = clean(memory && (memory.content || memory.summary)).replace(/\s+/g, ' ');
    return content ? (content.length > 88 ? content.slice(0, 87).trimEnd() + '…' : content) : 'Untitled memory';
  };
  const read = key => {
    try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (_) { return null; }
  };
  const write = (key, value) => {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) {}
  };
  const readJourney = key => {
    const value = read(key);
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  };
  const instructions = {
    codex: 'codex mcp add engraphis -- engraphis-mcp',
    claude: 'claude mcp add engraphis -- engraphis-mcp',
    generic: JSON.stringify({ mcpServers: { engraphis: { command: 'engraphis-mcp' } } }, null, 2),
  };

  window.EngraphisWorkflow = {
    title,
    create({ onProjectChange, onNavigate, onNewMemory }) {
      const byId = id => document.getElementById(id);
      let workspace = '';
      let project = '';
      let projects = [];
      const projectNames = new Map();
      const projectKey = () => 'engraphis-project-v1:' + encodeURIComponent(workspace);
      const journeyKey = () => 'engraphis-connection-journey-v1:'
        + encodeURIComponent(workspace) + ':' + encodeURIComponent(project);
      let journey = {};
      const checkIds = ['connection-configured', 'connection-recalled', 'connection-corrected'];
      const scopeLabel = () => project ? workspace + ' / ' + project : workspace + ' / all projects';

      function renderJourney() {
        const host = Object.hasOwn(instructions, journey.host) ? journey.host : 'codex';
        byId('connection-host').value = host;
        byId('connection-command').textContent = instructions[host];
        byId('connection-scope').textContent = workspace ? scopeLabel() : 'Choose a workspace to begin.';
        checkIds.forEach(id => {
          byId(id).checked = journey[id] === true;
          byId(id).disabled = !workspace;
        });
        const completed = checkIds.filter(id => journey[id] === true).length;
        byId('connection-progress').textContent = !workspace ? 'No workspace selected.'
          : completed + ' of 3 steps confirmed by you. Agent connection and restart are not verified by this dashboard.';
        byId('home-setup-progress').textContent = !workspace ? 'Create a workspace to begin setup.'
          : completed + ' of 3 setup steps confirmed by you.';
        const next = checkIds.findIndex(id => journey[id] !== true);
        const guidance = [
          'Next: connect Engraphis to your coding agent.',
          'Next: save a project fact and recall it after restarting your agent.',
          'Next: correct the fact and inspect its preserved history.',
        ];
        byId('home-setup-next').textContent = (next < 0 ? 'All checklist steps are marked by you.' : guidance[next])
          + ' Agent connection and restart are not verified by this dashboard.';
        byId('connection-add-memory').disabled = !workspace;
        byId('connection-ask').disabled = !workspace;
      }

      function renderProjects() {
        const select = byId('project-select');
        select.replaceChildren(new Option('All projects', ''));
        const names = [...new Set(projects.concat(project ? [project] : []))].sort();
        names.forEach(name => select.append(new Option(name, name)));
        select.value = project;
        select.disabled = !workspace;
        byId('project-name').disabled = !workspace;
        byId('project-apply').disabled = !workspace;
        document.querySelectorAll('[data-memory-context]').forEach(element => {
          element.textContent = workspace ? scopeLabel() : 'Choose a workspace';
        });
      }

      function chooseProject(value) {
        const selected = clean(value);
        if (!workspace || selected === project) return;
        project = selected;
        write(projectKey(), project);
        journey = readJourney(journeyKey());
        byId('project-name').value = '';
        byId('project-new').open = false;
        renderProjects();
        renderJourney();
        onProjectChange(project);
      }

      byId('project-select').addEventListener('change', event => chooseProject(event.target.value));
      byId('project-context-form').addEventListener('submit', event => {
        event.preventDefault();
        const value = byId('project-name').value.trim();
        if (value) chooseProject(value);
        else byId('project-name').focus();
      });
      byId('connection-host').addEventListener('change', event => {
        journey.host = event.target.value;
        if (workspace) write(journeyKey(), journey);
        renderJourney();
      });
      checkIds.forEach(id => byId(id).addEventListener('change', event => {
        journey[id] = event.target.checked;
        write(journeyKey(), journey);
        renderJourney();
      }));
      byId('connection-add-memory').addEventListener('click', onNewMemory);
      byId('connection-ask').addEventListener('click', () => onNavigate('ask'));
      byId('connection-copy').addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(byId('connection-command').textContent);
          byId('connection-copy-status').textContent = 'Copied setup command.';
        } catch (_) {
          byId('connection-copy-status').textContent = 'Copy is unavailable. Select and copy the command above.';
        }
      });
      return {
        project: () => project,
        ownership(memory, ownerWorkspace = workspace) {
          const owner = clean(memory.workspace_name) || clean(memory.workspace) || clean(ownerWorkspace)
            || 'workspace ownership unavailable';
          if (memory.scope === 'user') return 'User-wide';
          if (memory.scope === 'workspace') return owner + ' / workspace-wide';
          const projectName = clean(memory.repo_name) || clean(memory.repo)
            || (owner === workspace ? projectNames.get(memory.repo_id) : '');
          if (memory.scope === 'repo' || memory.repo_id || projectName) {
            return owner + ' / ' + (projectName || 'project ownership unavailable')
              + (memory.scope === 'session' ? ' / session' : '');
          }
          return owner + (memory.scope === 'session' ? ' / session' : ' / ownership unavailable');
        },
        selectWorkspace(name) {
          workspace = name || '';
          project = clean(read(projectKey()));
          projects = [];
          projectNames.clear();
          journey = readJourney(journeyKey());
          byId('project-name').value = '';
          byId('project-load-status').textContent = workspace ? 'Loading projects…' : '';
          renderProjects();
          renderJourney();
        },
        setProjects(values) {
          projectNames.clear();
          (Array.isArray(values) ? values : []).forEach(value => {
            if (value && clean(value.id) && clean(value.name)) projectNames.set(value.id, clean(value.name));
          });
          projects = (Array.isArray(values) ? values : []).map(value => clean(
            typeof value === 'string' ? value : value && value.name,
          )).filter(Boolean);
          byId('project-load-status').textContent = project && !projects.includes(project)
            ? 'This project name will be used for new memories.' : '';
          renderProjects();
        },
        projectsUnavailable() {
          projectNames.clear();
          byId('project-load-status').textContent = 'Project list unavailable. Your selected context is kept; you can enter a project name.';
        },
      };
    },
  };
})();
