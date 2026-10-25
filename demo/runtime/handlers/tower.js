// Demo Tower answers: state, threads, insights, models and a canned streamed reply chosen by keyword.
(function (self) {
  var R = self.DemoRouter;
  var F = self.DemoFleet;
  var PREV_THREAD = 'demo-earlier';
  var HOUR = 3600;

  function sec(ctx) { return Math.floor(ctx.now / 1000); }
  function round(v, d) { var k = Math.pow(10, d || 0); return Math.round(v * k) / k; }
  function model() { return F.byId('atlas').models[0]; }
  function names(id) { return F.byId(id).models.join(', '); }

  var ANSWERS = {
    slow: { tool: 'get_metrics', args: { host: 'forge.example' }, ms: 118,
      text: 'forge.example is the slow one.\n\n'
        + '- Its graphics card has been around 82 °C for the last 20 minutes, above the 80 °C warning level, so it slows itself down to cool off.\n'
        + '- It answers noticeably slower than it did earlier today.\n\n'
        + 'Sending new requests to atlas.example for a while would help, and so would cleaning the fans on forge.example.' },
    alarm: { tool: 'get_alerts', args: {}, ms: 84,
      text: 'There are two open alarms.\n\n'
        + '- The warning is on forge.example: its graphics card is a little over the 80 °C limit and has been for about 25 minutes.\n'
        + '- The other is a low-priority note on quill.example: it is using a little swap memory, around 6%.\n\n'
        + 'Neither one is stopping any work right now, but the forge.example one is worth watching.' },
    cost: { tool: 'get_energy', args: { window: '24h' }, ms: 133,
      text: 'atlas.example costs the most to run because it has two graphics cards, and quill.example costs the least.\n\n'
        + 'The machines draw far more power while they are answering requests than when they are idle, so the bill '
        + 'follows how busy they are.' },
    model: { tool: 'get_models', args: {}, ms: 97, text: '' },
    about: { text: 'LLM Systems Manager is built by LLM SysCore.\n\n'
      + '- It is an open-source project under the AGPL-3.0 license.\n'
      + '- The code is on GitHub at github.com/llmsyscore/llm-systems-manager.\n'
      + '- It runs on your own machines.' },
    help: { text: 'Here is where to get help.\n\n'
      + '- The guides are at https://www.llmsyscore.com/docs.html.\n'
      + '- For questions, email support@llmsyscore.com.\n'
      + '- Report bugs and ask for new features as GitHub issues at github.com/llmsyscore/llm-systems-manager/issues.\n\n'
      + 'Please send security problems privately by email instead of opening a public issue.' }
  };
  var MORE = {
    forecast: { tool: 'get_forecast', args: {}, ms: 102,
      text: 'The latest Forecast run found two things.\n\n'
        + '- Deal with forge.example first: its graphics card has been running about 1.5 °C hotter each week under the same load, '
        + 'and at that pace it reaches its limit in about six days. Clean the intake filters and check the fan curve.\n'
        + '- The second is a heads-up: the model disk on atlas.example is filling by about 18 GB a day and will be full in roughly two months. '
        + 'Removing models you no longer use fixes it.' },
    activity: { tool: 'get_activity', args: { window: '24h' }, ms: 141,
      text: 'In the last day the four machines were busy answering requests a little under 40% of the time.\n\n'
        + '- Six alarms went off: four cleared, and two are still open (a warm graphics card on forge.example and a swap note on quill.example).\n'
        + '- A report card ran on forge.example about 5 hours ago for Qwen2.5-14B.\n'
        + '- A benchmark ran on atlas.example about 9 hours ago for Llama-3.1-8B.\n\n'
        + 'Nothing failed.' },
    recent: { tool: 'get_alerts', args: { since_minutes: 60 }, ms: 77,
      text: 'Here is what happened in the last hour.\n\n'
        + '- About 25 minutes ago the graphics card on forge.example went over 80 °C and raised a warning, and alex acknowledged it a few minutes later.\n'
        + '- Earlier in the hour a model was loaded on forge.example.\n'
        + '- All four machines stayed online.' },
    engine: { tool: 'get_system_health', args: {}, ms: 69,
      text: 'Yes, the alarm engine is running and healthy.\n\n'
        + 'It is checking every machine on schedule, and it raised the forge.example temperature warning about 25 minutes ago, '
        + 'which shows alerts are getting through.' },
    offenders: { tool: 'get_alerts', args: { window: '30d' }, ms: 126,
      text: 'forge.example and atlas.example raise the most alarms, two each in the last day.\n\n'
        + '- Heat is the most common cause: three of the last six alarms were about temperature, two on forge.example and one on atlas.example.\n'
        + '- The others were a graphics memory warning on atlas.example, a memory warning on studio.example and a swap note on quill.example.' },
    red: { tool: 'get_alerts', args: {}, ms: 81,
      text: 'Only forge.example has a warning right now.\n\n'
        + '- Its graphics card is just over the 80 °C limit and has been for about 25 minutes, so it slows itself down to cool off.\n'
        + '- The other three machines are fine; quill.example only has a low-priority note about a little swap use.\n\n'
        + 'Cleaning the fans on forge.example should clear it.' },
    hardware: { tool: 'get_hosts', args: {}, ms: 93,
      text: 'Here is the hardware in the fleet.\n\n'
        + '- atlas.example has an AMD Ryzen 9 7950X, two NVIDIA RTX 4090 cards with 24 GB each and 128 GB of memory.\n'
        + '- forge.example has an AMD Ryzen 7 5800X3D, one RTX 3090 with 24 GB and 64 GB of memory.\n'
        + '- studio.example is a Mac with an Apple M2 Ultra and 192 GB of shared memory.\n'
        + '- quill.example has an Intel Core i5-12400 and 32 GB of memory but no graphics card, so it runs its small model on the processor.' },
    idle: { tool: 'get_energy', args: { window: '24h' }, ms: 112,
      text: 'All four machines spend about 60% of their time idle, waiting for requests.\n\n'
        + '- Idle time still uses power: it is a little under a third of the energy bill.\n'
        + '- atlas.example wastes the most while idle, about 150 W, because of its two graphics cards.\n'
        + '- studio.example wastes the least, about 20 W.' },
    mtok: { tool: 'get_energy', args: { window: 'today' }, ms: 108,
      text: 'Across the fleet, a million words cost about $0.77 in electricity, or about $0.56 if you count only the time the machines were busy.\n\n'
        + '- studio.example is the cheapest at about $0.39.\n'
        + '- quill.example is the most expensive at about $1.11, because it is slow.' },
    power: { tool: 'get_energy', args: { window: 'today' }, ms: 119,
      text: 'atlas.example uses the most power, about half of the total: around 340 W on average, or 8 kWh a day.\n\n'
        + '- forge.example comes next at about 190 W.\n'
        + '- Then studio.example at about 65 W and quill.example at about 50 W.\n\n'
        + 'Together they use about 15 kWh a day, roughly $2.30 at $0.15 per kWh.' },
    unused: { tool: 'get_models', args: {}, ms: 95,
      text: 'No. Every loaded model has answered requests in the last hour.\n\n'
        + 'atlas.example also keeps Qwen2.5-32B downloaded but not loaded, so it uses disk space and no memory.' },
    big: { tool: 'get_benchmarks', args: {}, ms: 137,
      text: 'atlas.example is the best place for a 27B model.\n\n'
        + '- Its two RTX 4090 cards have 48 GB together, and in benchmarks the 32B Qwen model ran there at about 36 words per second.\n'
        + '- studio.example has the memory for it too, but its Apple chip answers more slowly.\n'
        + '- forge.example has only one 24 GB card, so a 27B model would barely fit.\n'
        + '- quill.example has no graphics card.' },
    fastest: { tool: 'get_benchmarks', args: {}, ms: 124,
      text: 'Llama-3.1-8B on atlas.example is the fastest.\n\n'
        + 'In benchmarks it writes about three times as fast as the bigger Qwen2.5-32B on the same machine, and it reads prompts quickest too.' },
    slots: { tool: 'get_llama_state', args: { host: 'atlas.example' }, ms: 88,
      text: 'A slot is one request a model server can work on at the same time.\n\n'
        + 'atlas.example has 4 slots and usually only one is busy, with nothing waiting, so there is no slot pressure.\n\n'
        + 'Pressure means all slots are busy and new requests have to wait in line; if that happens often, add slots or spread the work to another machine.' },
    versions: { tool: 'get_hosts', args: {}, ms: 74,
      text: 'All four agents are on ' + (self.DEMO_RELEASE || 'the latest release') + ', the same version as the manager.\n\n'
        + 'No updates are waiting.' },
    offline: { tool: 'get_hosts', args: {}, ms: 71,
      text: 'No, nothing is offline.\n\n'
        + 'All four machines (atlas.example, forge.example, studio.example and quill.example) are online and reporting every few seconds.' },
    bench: { tool: 'get_tool_runs', args: {}, ms: 103,
      text: 'The last benchmark ran on atlas.example about 9 hours ago with Llama-3.1-8B.\n\n'
        + '- It wrote about 68 words per second and read prompts at about 1,540 words per second.\n'
        + '- Autotune finished a run two days ago on atlas.example for the same model and found settings about 12% faster.\n'
        + '- Another autotune run is going on atlas.example now.' }
  };
  Object.keys(MORE).forEach(function (k) { ANSWERS[k] = MORE[k]; });
  ANSWERS.model.text = 'atlas.example is running ' + F.byId('atlas').models[0] + ' and also has '
    + F.byId('atlas').models[1] + ' and ' + F.byId('atlas').models[2] + ' available.\n\n'
    + '- forge.example has ' + names('forge') + '.\n'
    + '- studio.example has ' + names('studio') + '.\n'
    + '- quill.example runs the small ' + names('quill') + '.\n\n'
    + 'Each machine answers on its own, so you can pick the one that fits the job.';

  // General product answers, written from the README and docs.
  var PRODUCT = {
    whatis: { text: 'LLM Systems Manager is one self-hosted dashboard to monitor, control, benchmark and tune every machine that runs your local LLMs.\n\n'
      + '- It shows live numbers for each machine: graphics card, power, temperature and how fast your models answer.\n'
      + '- You can start servers, swap models, edit settings and read logs from the browser.\n'
      + '- It sends alerts by email, webhook or Discord when something goes wrong.\n'
      + '- It benchmarks and tunes your models, and shows what your inference costs in electricity.\n'
      + '- Tower, the built-in assistant, answers questions about all of it in plain language.' },
    whofor: { text: 'It is for anyone who runs AI models on their own machines and wants to see and manage them in one place.\n\n'
      + '- If you have one machine or several, every one shows up in the same dashboard.\n'
      + '- Operators can manage models and watch the dashboards; admins also manage machines and settings.\n'
      + '- You do not need to be an expert: Tower answers questions in plain language.' },
    how: { text: 'Four parts work together.\n\n'
      + '- The manager hosts the web dashboard and sends your commands to each machine.\n'
      + '- An agent on each machine reads its sensors every 5 seconds and can start, stop and configure your model servers.\n'
      + '- The alarm engine checks every reading against your alarm rules and sends the notifications.\n'
      + '- InfluxDB, a database, stores the history so the dashboard can draw charts.\n\n'
      + 'They talk to each other over encrypted connections on your local network.' },
    servers: { text: 'It works with llama.cpp, vLLM and LM Studio, and it can run stable-diffusion.cpp for image generation.\n\n'
      + '- Graphics cards: NVIDIA, AMD (ROCm and Vulkan) and Apple Silicon.\n'
      + '- Machines: Linux and macOS.\n'
      + '- The agent finds what each machine is running and turns on only the features that fit it.\n'
      + '- One address in front of all of them lets your apps use every server.' },
    needs: { text: 'You need a Linux machine for the dashboard and, optionally, an agent on each machine you want to watch.\n\n'
      + '- The dashboard runs on Debian, Ubuntu, and Rocky Linux, AlmaLinux or RHEL 10, or on macOS through Homebrew.\n'
      + '- It needs Python 3.11 or newer, at least 2 GB of memory (4 GB is better) and 10 GB of free disk.\n'
      + '- Agents run on Linux (Ubuntu 22.04 or newer) and macOS 13 or newer.\n'
      + '- The installer checks the rest and offers to install what is missing.\n\n'
      + 'Windows is not supported.' },
    alarms: { text: 'An alarm engine checks every reading from every machine against your alarm rules.\n\n'
      + '- Rules can watch a fixed limit, like a temperature, or look for unusual behavior.\n'
      + '- When a rule triggers, you get a notification by email, webhook, Discord or a message in the dashboard.\n'
      + '- Several rules firing on one machine at once become a single incident with one notification.\n'
      + '- The Events tab lists open alarms and the history, and the phone app sends push alerts.\n\n'
      + 'Tower can also look into new alarms and suggest what to do.' },
    autotune: { text: 'Autotune tries different settings on a model server and keeps the ones that work best for your goal.\n\n'
      + '- You pick a goal: Fit, Speed, Balanced or Serve.\n'
      + '- It tunes seven settings in order, from context size and KV cache to speculative decoding and parallel slots.\n'
      + '- It then checks the result under real traffic and shows a before and after table.\n'
      + '- It works on llama.cpp, LM Studio and vLLM machines, and can run overnight.' },
    whatforecast: { text: 'Forecast looks at the history your machines have reported and warns you about trouble before it becomes an alarm.\n\n'
      + '- It runs sixteen trend checks, such as disks filling, memory running short, temperatures rising and speed dropping.\n'
      + '- Each finding shows a date it is heading toward and a suggested next step.\n'
      + '- A 30-day outlook and a briefing by machine or check show everything in one place.\n'
      + '- Every finding has an Ask Tower button, and findings can also raise alerts.\n\n'
      + 'You choose how often it runs in Settings.' },
    benchmarks: { text: 'Benchmarks measure how fast a model runs on your machine, and the GPU Report Card is one standard test you can compare across machines.\n\n'
      + '- A live benchmark tests the server you are running now, with different workloads and numbers of simultaneous requests.\n'
      + '- An offline benchmark tests models across your library.\n'
      + '- A Report Card shows time to first word, speed, energy per word, cost per million words and the graphics card used.\n'
      + '- Results are saved, so you can compare runs and see changes over time.\n\n'
      + 'You find them in the Tools tab.' },
    energy: { text: 'It works out what your inference costs from the power your machines really draw.\n\n'
      + '- You see the cost per million words, measured from real power use.\n'
      + '- It shows how much you save each month compared with hosted API prices.\n'
      + '- Idle time is counted too, so you see what waiting for requests costs.\n'
      + '- The Energy tab breaks energy, cost and words down by machine and by model.\n\n'
      + 'You set your electricity price in the settings.' },
    tower: { text: 'Tower is the built-in assistant: ask about your machines, models, alarms and energy in plain language.\n\n'
      + '- It reads machine details, history, loaded models, alarms, energy, recent runs and logs.\n'
      + '- It can also load or unload a model, acknowledge an alert, start a benchmark or restart a server, but each action waits for your approval.\n'
      + '- It uses a chat model already loaded on your own machines; 7B or larger is recommended.\n'
      + '- It starts in answer-only mode, and an admin can allow actions in Settings.\n\n'
      + 'It answers from your own dashboard data.' },
    phone: { text: 'Yes, the dashboard includes a phone app that installs from your browser.\n\n'
      + '- Open the dashboard\'s /companion page on your phone, sign in and choose Add to Home Screen.\n'
      + '- It has Home, Alerts, Tower, Energy, Models, Admin and Settings screens sized for a phone.\n'
      + '- Alarms arrive as push notifications even when the app is closed.\n'
      + '- Model swaps and restarts ask you to confirm first, and only admins can run them.\n\n'
      + 'It needs a trusted HTTPS address for the dashboard.' },
    addhost: { text: 'Install the agent on each machine you want to watch, then approve it in the dashboard.\n\n'
      + '- Run the same install command on that machine and pick mode 5 (agent), or copy the ready-made command from Admin, Agents, Manage, Enrollment.\n'
      + '- Then approve the machine under Admin, Agents.\n'
      + '- Agents run on Linux and macOS, and Homebrew and a standalone agent binary are available too.\n'
      + '- New machines are accepted while enrollment is open, which it is for 15 minutes after the dashboard starts or until you close it.' },
    install: { text: 'One command sets everything up on a Linux machine: get it at https://get.llmsyscore.com/?ref=demo.\n\n'
      + '- It checks the basics, downloads the latest release and verifies it.\n'
      + '- It installs InfluxDB, writes the settings file and creates the certificates.\n'
      + '- It asks a few questions, such as the admin login and email settings for alerts.\n'
      + '- Docker Compose, .deb and .rpm packages and Homebrew are also available.\n\n'
      + 'When it finishes it prints the dashboard address and your first sign-in details.' },
    free: { text: 'It is open source under the AGPL-3.0 license, and you run it on your own machines.\n\n'
      + '- The code is on GitHub at github.com/llmsyscore/llm-systems-manager.\n'
      + '- Issues and pull requests are welcome.\n'
      + '- If you find it useful, the project accepts donations.' },
    privacy: { text: 'The installer asks once whether to send a few anonymous reports.\n\n'
      + '- The install report, sent once per install or update, has the version, operating system, CPU model, memory size and graphics card vendor and count.\n'
      + '- A crash report names the error type and where in the code it happened, at most five a day.\n'
      + '- A daily check-in has the version, operating system and install channel.\n'
      + '- A more detailed daily report is available if you turn it on.\n\n'
      + 'Reports carry no names, addresses, paths, tokens or model names. Turn any of them off in Settings under Telemetry & analytics, or set DO_NOT_TRACK=1.' },
    update: { text: 'Run the installer with the --update option on the dashboard machine, then update your agents.\n\n'
      + '- Homebrew and package installs update with brew upgrade or your package manager.\n'
      + '- The updater backs up the files that change, restarts the affected services and checks that all is well.\n'
      + '- Update the dashboard machine first, then the alarm engine machine, then the agents.\n'
      + '- Agents update from the dashboard: use Update on one agent, or Update all under Admin, Agents, Manage.\n\n'
      + 'The Admin tab shows an amber dot when a new release is available.' },
    routing: { text: 'Yes, one OpenAI-compatible address sends each request to a machine that serves the model you asked for.\n\n'
      + '- Your apps point at the dashboard instead of at one server.\n'
      + '- It picks the machine by a pin you set for the model, then takes turns between machines serving it.\n'
      + '- If a machine cannot be reached before the first word, it tries another one with the same model.\n'
      + '- Outside apps use API keys; Model Autopilot keeps your chosen models loaded and moves them if a machine goes down.' },
    users: { text: 'Yes, there are two roles, Admin and Operator.\n\n'
      + '- Operators can manage models and watch the dashboards.\n'
      + '- Admins also get the Admin tab, agent management, secrets and settings.\n'
      + '- The installer gives you a temporary admin password for the first sign-in, and each person can change their own.\n'
      + '- Repeated failed sign-ins are locked out, and the Audit Log records every admin action.' }
  };
  Object.keys(PRODUCT).forEach(function (k) { ANSWERS[k] = PRODUCT[k]; });

  // Evenly spaced [unix_s, value] points ending at nowS; fn(i, n) gives each value.
  function seriesPts(nowS, n, stepS, fn) {
    var pts = [];
    for (var i = 0; i < n; i++) pts.push([nowS - (n - 1 - i) * stepS, round(fn(i, n), 1)]);
    return pts;
  }
  function histRow(host, pts, unit, metric, threshold) {
    var c = { points: pts, unit: unit, metric: metric, host: host };
    if (threshold) c.threshold = threshold;
    return { host: host, _chart: c };
  }
  function tempPts(nowS) {
    return seriesPts(nowS, 48, 1800, function (i) {
      return 73 + 2.5 * Math.sin(i * Math.PI / 24) + 9 * Math.max(0, (i - 40) / 7);
    });
  }
  function speedPts(nowS, base, dip) {
    return seriesPts(nowS, 48, 1800, function (i) {
      return base + 1.2 * Math.sin(i * 0.9) - dip * Math.max(0, (i - 40) / 7);
    });
  }
  function powerPts(nowS, base, amp) {
    return seriesPts(nowS, 48, 1800, function (i) { return base + amp * Math.sin(i * Math.PI / 24 + 1); });
  }
  function watchSeries(nowS) {
    return seriesPts(nowS, 21, 60, function (i) { return 81 + 0.7 * Math.sin(i * 0.7) + 0.1 * (i % 3); });
  }

  var TRENDS = {
    trendtemp: { tool: 'host_history', args: { host: 'forge.example', metric: 'gpu_temperature_c', window: '24h' }, ms: 146,
      result: function (s) { return { ok: true, hosts: [histRow('forge.example', tempPts(s), '°C', 'GPU temperature', 80)] }; },
      text: 'The graphics card on forge.example has been getting warmer over the last day.\n\n'
        + '- For most of the day it stayed around 70 to 76 °C, rising and falling with the daily workload.\n'
        + '- It has climbed since then and has been above the 80 °C warning level for the last 20 to 25 minutes.\n'
        + '- It now sits around 81 °C, which is why the warning is open.\n\n'
        + 'Cleaning the fans on forge.example should bring it back down.' },
    trendspeed: { tool: 'host_history', args: { host: 'atlas.example', metric: 'generation_speed', window: '24h' }, ms: 152,
      result: function (s) {
        return { ok: true, hosts: [histRow('atlas.example', speedPts(s, 68, 0), 'words/s', 'generation speed'),
          histRow('forge.example', speedPts(s, 52, 14), 'words/s', 'generation speed')] };
      },
      text: 'Here is how fast atlas.example and forge.example answered over the last day.\n\n'
        + '- atlas.example stayed steady at about 68 words per second.\n'
        + '- forge.example slowed down toward the end of the day, when its graphics card got hot.\n\n'
        + 'Sending new requests to atlas.example for a while would help.' },
    trendpower: { tool: 'host_history', args: { host: 'atlas.example', metric: 'power_w', window: '24h' }, ms: 149,
      result: function (s) {
        return { ok: true, hosts: [histRow('atlas.example', powerPts(s, 340, 45), 'W', 'power'),
          histRow('forge.example', powerPts(s, 190, 25), 'W', 'power')] };
      },
      text: 'Here is the power use of atlas.example and forge.example over the last day.\n\n'
        + '- atlas.example drew around 340 W on average.\n'
        + '- forge.example drew around 190 W on average.\n\n'
        + 'Both follow how busy they are, rising during the day and falling at night.' },
    watch: { tool: 'timer', args: { host: 'forge.example' }, ms: 163,
      result: function (s) {
        return { ok: true, series: watchSeries(s), unit: '°C', metric: 'GPU temperature · forge.example' };
      },
      text: 'I watched the graphics card on forge.example for 20 minutes.\n\n'
        + '- It stayed between 80 and 82 °C the whole time, just above the 80 °C warning level.\n'
        + '- It did not cool down on its own.\n\n'
        + 'On a real install, Tower can keep watching a number and tell you when it changes.' }
  };
  Object.keys(TRENDS).forEach(function (k) { ANSWERS[k] = TRENDS[k]; });

  var FALLBACK = 'Some answers are limited in this demo, which runs on sample data.\n\n'
    + 'Try one of these:\n\n'
    + '- Which machine is slow?\n'
    + '- Any open alarms?\n'
    + '- Show GPU temperature on forge.example over the last day\n'
    + '- What can this do?\n'
    + '- How do alarms work?\n'
    + '- How do I install it?\n\n'
    + 'For anything else, email support@llmsyscore.com or visit https://www.llmsyscore.com.';

  // General product questions, checked before the fleet questions.
  var PRODUCT_KEYS = [
    ['whatforecast', /\bwhat is (a |the )?forecast|how does (the )?forecast|explain forecast/],
    ['autotune', /(how|what|explain).*autotune|autotune.*(work|do\b)/],
    ['benchmarks', /(what is|what are|how do|how does|explain).*(benchmark|report card)/],
    ['privacy', /privacy|telemetry|anonymous|what data|data leave|phone home|do_not_track/],
    ['tower', /(what|how|which|can).*\btower\b.*(do|work|model|use|run)|what is tower|about tower/],
    ['how', /how (does|do) (it|this|llm systems manager) work|how is it built/],
    ['phone', /phone app|companion|\bpwa\b|mobile app|on (my|a) (phone|mobile)|\bphone\b.*\bapp\b/],
    ['addhost', /\b(add|connect|enroll|register|set up)\b.*\b(machines?|hosts?|agents?|computers?)\b|what is an agent/],
    ['update', /how.*\b(update|upgrade)\b|\bupgrad/],
    ['install', /\binstall(ing|ation)?\b|get started|set ?up (it|this|llm systems manager)\b|set (it|this) up/],
    ['needs', /what do i need|requirements?|windows|which (operating systems?|platforms?)|(does|can) (it|this|llm systems manager) run on|what does (it|this) run on/],
    ['servers', /(which|what) (model )?(servers?|backends?|providers?)\b|(does|do) (it|this|llm systems manager) (support|work with)|(support|work with) (llama|lm studio|vllm|ollama|nvidia|amd|apple|gpus?)|compatible with/],
    ['whofor', /\bwho is (this|it|llm systems manager) for\b|who (should|would|can) use/],
    ['alarms', /how do(es)? (the |my )?(alarms?|alerts?|alerting|notifications?) work|how.*(notified|alerted)|(what is|what are) (an? )?(alarms?|alerts?)\b/],
    ['energy', /(track|measure).*(energy|cost|electricity)|how.*(energy|cost|electricity).*(work|tracked|measured|calculated)/],
    ['routing', /\brout(e|es|ing)\b|gateway|load.?balanc|openai/],
    ['users', /access control|(multiple|several|more) users|user roles|\broles?\b|permissions?|password|(add|invite|manage) (a |more )?users?/],
    ['free', /\b(is|are) (it|this|llm systems manager) free\b|licen[sc]e|open.?source|pricing|how much does (it|this|llm systems manager) cost/],
    ['whatis', /what is (llm systems manager|this|it|the product)\b|what (can|does) (this|it|llm systems manager) do|what can llm systems manager do/]
  ];
  function productKey(t) {
    for (var i = 0; i < PRODUCT_KEYS.length; i++) if (PRODUCT_KEYS[i][1].test(t)) return PRODUCT_KEYS[i][0];
    return null;
  }

  // Picks the canned answer key for a question; null means the fallback.
  function keyFor(text) {
    var t = String(text || '').toLowerCase();
    var pk = productKey(t);
    if (pk) return pk;
    var trend = /over time|trend|last day|last 24|\bcharts?\b|\bgraphs?\b|history/.test(t);
    if (/forecast/.test(t)) return 'forecast';
    if (/\bwho\b/.test(t) && /develop|make|made|build|built|maintain|behind|creat/.test(t)) return 'about';
    if (/\b(help|support|contact)\b|\bbugs?\b/.test(t)) return 'help';
    if (trend && /temperature|\btemp\b|\bhot\b/.test(t)) return 'trendtemp';
    if (trend && /\bspeed\b|tokens? per second|t\/s|tok\/s/.test(t)) return 'trendspeed';
    if (trend && /power|energy|watts?/.test(t)) return 'trendpower';
    if (/activity summary/.test(t)) return 'activity';
    if (/what changed|last hour/.test(t)) return 'recent';
    if (/alarm engine/.test(t)) return 'engine';
    if (/\bwatch\b|keep an eye|\bmonitor\b/.test(t)) return 'watch';
    if (/alarms the most|offender|alarm rules|per day/.test(t)) return 'offenders';
    if (/slow/.test(t)) return 'slow';
    if (/\bred\b/.test(t)) return 'red';
    if (/alarm|alert/.test(t)) return 'alarm';
    if (/hardware|\bcpu\b|\bgpu\b/.test(t)) return 'hardware';
    if (/\bidle\b/.test(t)) return 'idle';
    if (/mtok/.test(t)) return 'mtok';
    if (/cost/.test(t)) return 'cost';
    if (/power|energy/.test(t)) return 'power';
    if (/nobody|unused/.test(t)) return 'unused';
    if (/\b\d+b\b/.test(t)) return 'big';
    if (/fastest/.test(t)) return 'fastest';
    if (/slot/.test(t)) return 'slots';
    if (/version/.test(t)) return 'versions';
    if (/offline/.test(t)) return 'offline';
    if (/benchmark|autotune/.test(t)) return 'bench';
    if (/model/.test(t)) return 'model';
    return null;
  }

  function stateBody() {
    var m = model();
    return {
      ok: true, enabled: true, admin: true, model: m, provider: 'llama', hosts: ['atlas.example'],
      check: { model: m, grade: 'native', mode: 'native', size_b: 8 },
      thinking_options: null, fallback: null, fallback_enabled: false,
      capabilities: 'read', off_topic: 'refuse', diagnose_alarms: true,
      insights_new: 1, latest_insight: null, insights_rev: 1, timers: 0
    };
  }

  function alertList(nowMs) {
    return self.DemoAlarms ? self.DemoAlarms.alerts(nowMs) : [];
  }
  function createdS(a) { return Math.floor(Date.parse(a.created_at + 'Z') / 1000); }

  function snapshot(nowS) {
    var pts = [];
    for (var i = 0; i < 30; i++) {
      var ts = nowS - (29 - i) * 120;
      pts.push([ts, round(74 + 8.4 * Math.min(1, i / 18) + 0.8 * Math.sin(i * 1.3), 1)]);
    }
    return { points: pts, threshold: 80, unit: '°C', metric: 'gpu_temperature_c', minutes: 60 };
  }

  function insightRows(ctx) {
    var s = sec(ctx);
    var al = alertList(ctx.now);
    var notes = [
      { status: 'new', summary: 'The graphics card on forge.example is running hot and has slowed down.',
        detail: 'The card has been just above 80 °C for about 25 minutes. Speed drops when it gets this warm.',
        action: 'Check the case fans on forge.example, or send new requests to atlas.example for a while.',
        checks: [{ ok: true, name: 'get_metrics', summary: 'GPU temperature 82 °C, fan speed steady' },
                 { ok: true, name: 'get_alerts', summary: 'One warning open on forge.example' }], snap: true },
      { status: 'seen', summary: 'quill.example is using a little swap memory.',
        detail: 'Swap use is around 6%. The machine has little memory, so this is expected while it answers.',
        action: 'Nothing to do unless it keeps growing.', checks: [] },
      { status: 'seen', summary: 'The graphics card on atlas.example got too hot for a few minutes and then cooled down.',
        detail: 'It peaked at 86 °C and cleared on its own after about 9 minutes.',
        action: 'No action needed.', checks: [] }
    ];
    var mine = R.state().get('insights', {});
    return notes.map(function (n, i) {
      var a = al[i] || {};
      var at = a.created_at ? createdS(a) : s - 3600 * (i + 1);
      var id = 'demo-ins-' + (i + 1);
      var my = mine[id] || {};
      return {
        id: id, alert_id: a.alert_id || '', rule: a.rule_name || 'Alert', host: a.source_host || '',
        severity: a.severity || 'warning', summary: n.summary, detail: n.detail, suggested_action: n.action,
        playbook_id: null, playbook_title: null, playbook_safe: null, steps: [], checks: n.checks, thread_id: null,
        status: my.status || n.status, created: at, resolved: my.resolved || null, applied_by: my.applied_by || null,
        result: my.result || null, seen_at: n.status === 'seen' ? at + 120 : null, snapshot: n.snap ? snapshot(s) : null
      };
    });
  }
  function openInsights(ctx) {
    return insightRows(ctx).filter(function (r) { return r.status !== 'dismissed'; });
  }
  function newCount(rows) { return rows.filter(function (r) { return r.status === 'new'; }).length; }

  function thread(id, title, created, updated) {
    return { id: id, title: title, created: created, updated: updated, page: {} };
  }
  function prevThread(ctx) {
    var s = sec(ctx);
    return thread(PREV_THREAD, 'Which machine is busiest?', s - 3 * HOUR, s - 3 * HOUR + 40);
  }
  function prevMessages(ctx) {
    var s = sec(ctx) - 3 * HOUR;
    return [
      { role: 'user', content: 'Which machine is busiest right now?', tool_name: null, tool_args: null, tool_ok: null, tool_ms: null, ts: s },
      { role: 'tool', content: JSON.stringify({ ok: true, hosts: 4 }), tool_name: 'get_fleet_status', tool_args: '{}', tool_ok: 1, tool_ms: 92, ts: s + 5 },
      { role: 'assistant', tool_name: null, tool_args: null, tool_ok: null, tool_ms: null, ts: s + 12,
        content: 'atlas.example is the busiest. It is answering requests with both of its graphics cards and its speed '
          + 'is steady. forge.example is next, running a little warm. studio.example and quill.example are mostly idle.' }
    ];
  }

  R.route('GET /api/tower/state', function (ctx) {
    var st = stateBody();
    var rows = self.DemoAlarms ? openInsights(ctx) : [];
    var ins = rows[0] || null;
    st.insights_new = newCount(rows);
    st.latest_insight = ins ? { id: ins.id, rule: ins.rule, host: ins.host, severity: ins.severity, summary: ins.summary, created: ins.created } : null;
    return st;
  });
  R.route('GET /api/tower/timers', function () { return { ok: true, timers: [] }; });
  R.route('GET /api/tower/insights', function (ctx) {
    var rows = insightRows(ctx);
    return { ok: true, insights: rows, new: newCount(rows) };
  });

  R.route('GET /api/tower/eval', function (ctx) {
    var s = sec(ctx);
    var cases = [
      ['alerts', 'Open alarms', 1, 62], ['hosts', 'Fleet status', 1, 71], ['metrics', 'One machine’s numbers', 2, 148],
      ['models', 'Loaded models', 1, 80], ['timer', 'Watch a number for a while', 2, 190], ['off', 'A question about something else', 0, 40]
    ].map(function (c) {
      return { id: c[0], title: c[1], passed: true, calls: c[2], corrections: 0, retries: 0, ms: c[3] * 10, detail: '' };
    });
    var row = { id: 'demo-eval-1', model: model(), provider: 'llama', quant: 'Q8_0', server: 'llama.cpp', tool_mode: 'native',
      grade: 'native', at: s - 2 * 86400, ms: 41000, passed: 6, total: 6, calls: 7, corrections: 0, retries: 0, score_pct: 100,
      actor: 'alex', hosts: ['atlas.example'], cases: cases };
    return { ok: true, results: [row], live: null, model: model(), admin: true };
  });

  var CURATED = [
    ['qwen35-9b-q4', 'Qwen3.5 9B', 8, 'unsloth/Qwen3.5-9B-GGUF', 'Qwen3.5-9B-Q4_K_M.gguf', 'Q4_K_M', 5.7, 9, 'high', false],
    ['gemma4-12b-q6', 'Gemma 4 12B (Q6)', 16, 'unsloth/gemma-4-12b-it-GGUF', 'gemma-4-12b-it-Q6_K.gguf', 'Q6_K', 9.8, 12, 'high', false],
    ['qwen35-4b-q4', 'Qwen3.5 4B', 6, 'unsloth/Qwen3.5-4B-GGUF', 'Qwen3.5-4B-Q4_K_M.gguf', 'Q4_K_M', 2.7, 4, 'good', true]
  ];
  R.route('GET /api/tower/models', function () {
    var models = CURATED.map(function (c) {
      return { key: c[0], name: c[1], tier_gb: c[2], repo: c[3], file: c[4], quant: c[5], size_gb: c[6], params_b: c[7],
        expected: c[8], small: c[9], notes: 'Works well for Tower on a card of this size.', measured: '8 of 8, 2026-09-24',
        model_id: c[3] + ':' + c[5], present: false, loaded: false, eval: null, evals: { llama: null, lms: null } };
    });
    return { ok: true, models: models,
      hosts: [{ provider: 'llama', label: 'llama.cpp', host: 'atlas.example', agent_id: 'atlas', primary: true }],
      host: 'atlas.example', live: null, last: null,
      index: [{ id: model(), provider: 'llama', hosts: ['atlas.example'], loaded: true }], admin: true };
  });

  // Threads: literal path first, then the id route.
  // Renamed and deleted conversations are kept in this browser.
  function threadState() { return R.state().get('threads', { titles: {}, deleted: {} }); }
  function known(id) { return id === PREV_THREAD || id.indexOf('demo-t') === 0; }
  function notFound(msg) {
    return new Response(JSON.stringify({ ok: false, error: msg }), { status: 404, headers: { 'Content-Type': 'application/json' } });
  }
  R.route('GET /api/tower/threads', function (ctx) {
    var t = prevThread(ctx);
    var ts = threadState();
    var list = ts.deleted[t.id] ? [] : [{ id: t.id, title: ts.titles[t.id] || t.title, created: t.created, updated: t.updated }];
    return { ok: true, threads: list, discord: [] };
  });
  R.route('POST /api/tower/threads', function (ctx) {
    var s = sec(ctx);
    return { ok: true, thread: thread('demo-t' + s, (ctx.body && ctx.body.title) || 'New thread', s, s) };
  }, { read: true });
  R.route('GET /api/tower/threads/:tid', function (ctx) {
    var id = ctx.params.tid;
    var ts = threadState();
    if (!known(id) || ts.deleted[id]) return notFound('unknown thread');
    if (id === PREV_THREAD) {
      var p = prevThread(ctx);
      if (ts.titles[id]) p.title = ts.titles[id];
      return { ok: true, thread: p, messages: prevMessages(ctx), read_only: false, active_run: null };
    }
    var s = sec(ctx);
    return { ok: true, thread: thread(id, ts.titles[id] || 'New thread', s, s), messages: [], read_only: false, active_run: null };
  });
  R.route('PATCH /api/tower/threads/:tid', function (ctx) {
    var id = ctx.params.tid;
    var title = String((ctx.body && ctx.body.title) || '').trim().slice(0, 60);
    var ts = threadState();
    if (!known(id) || ts.deleted[id]) return notFound('unknown thread');
    if (!title) {
      return new Response(JSON.stringify({ ok: false, error: 'title required' }), { status: 400, headers: { 'Content-Type': 'application/json' } });
    }
    ts.titles[id] = title;
    var s = sec(ctx);
    return R.state().set('threads', ts).then(function () { return { ok: true, thread: thread(id, title, s, s) }; });
  }, { read: true });
  R.route('DELETE /api/tower/threads/:tid', function (ctx) {
    var id = ctx.params.tid;
    var ts = threadState();
    if (!known(id) || ts.deleted[id]) return notFound('unknown thread');
    ts.deleted[id] = true;
    return R.state().set('threads', ts).then(function () { return { ok: true }; });
  }, { read: true });
  R.route('POST /api/tower/threads/:tid/messages', function (ctx) {
    var text = ctx.body && ctx.body.text;
    if (!String(text || '').trim()) {
      return new Response(JSON.stringify({ ok: false, error: 'text required' }), { status: 400, headers: { 'Content-Type': 'application/json' } });
    }
    return { ok: true, run_id: 'demo-run-' + (keyFor(text) || 'other') + '-' + ctx.now };
  }, { read: true });

  // Splits a reply into small word groups so it types in like a live answer.
  function chunks(text) {
    var words = text.split(' ');
    var out = [];
    for (var i = 0; i < words.length; i += 3) out.push(words.slice(i, i + 3).join(' ') + (i + 3 < words.length ? ' ' : ''));
    return out;
  }

  // Event list for one run: thinking, an optional tool tick, the text in pieces, then done.
  function runEvents(key, nowS) {
    var a = ANSWERS[key];
    var ev = [{ event: 'status', state: 'thinking' }];
    if (a && a.tool) {
      ev.push({ event: 'tool', name: a.tool, ok: true, ms: a.ms,
        summary: 'read ' + a.tool.replace(/_/g, ' ') + (a.args.host ? ' · ' + a.args.host : '') + ' · ' + a.ms + ' ms',
        result: a.result ? a.result(nowS) : { ok: true } });
    }
    chunks(a ? a.text : FALLBACK).forEach(function (c) { ev.push({ event: 'delta', text: c }); });
    ev.push({ event: 'done', elapsed_ms: 1800 });
    return ev;
  }

  R.route('GET /api/tower/runs/:rid/stream', function (ctx) {
    var m = /^demo-run-([a-z]+)-/.exec(ctx.params.rid);
    var events = runEvents(m ? m[1] : 'other', sec(ctx));
    return self.DemoSse.stream(function (nowMs, i) {
      return i < events.length ? { data: events[i] } : null;
    }, 140);
  });

  // Page-fired writes (insights opened, panel closed) are answered quietly.
  R.route('POST /api/tower/insights/seen', function () { return { ok: true, seen: 0 }; }, { read: true });
  R.route('POST /api/tower/runs/:rid/park', function () { return { ok: true }; }, { read: true });
  R.route('POST /api/tower/runs/:rid/stop', function () { return { ok: true }; }, { read: true });

  // Dismissed and applied insights are kept in this browser; literal path before the id routes.
  function setInsights(mine, out) {
    return R.state().set('insights', mine).then(function () { return out; });
  }
  R.route('POST /api/tower/insights/dismiss_all', function (ctx) {
    var mine = R.state().get('insights', {});
    var n = 0;
    insightRows(ctx).forEach(function (r) {
      if (r.status === 'dismissed' || r.status === 'applying') return;
      mine[r.id] = { status: 'dismissed', resolved: sec(ctx), applied_by: r.applied_by, result: r.result };
      n++;
    });
    return setInsights(mine, { ok: true, dismissed: n });
  }, { read: true });
  function insightOp(ctx, to, allow) {
    var row = insightRows(ctx).filter(function (r) { return r.id === ctx.params.iid; })[0];
    if (!row) return notFound('unknown insight');
    if (allow.indexOf(row.status) < 0) {
      return new Response(JSON.stringify({ ok: false, error: 'not open' }), { status: 409, headers: { 'Content-Type': 'application/json' } });
    }
    var mine = R.state().get('insights', {});
    var my = { status: to, resolved: sec(ctx), applied_by: row.applied_by, result: row.result };
    if (to === 'applied') { my.applied_by = 'tower via alex'; my.result = { ok: true, message: 'done', steps: [] }; }
    mine[row.id] = my;
    return setInsights(mine, to === 'applied' ? { ok: true, status: 'applied', result: my.result } : { ok: true });
  }
  R.route('POST /api/tower/insights/:iid/dismiss', function (ctx) {
    return insightOp(ctx, 'dismissed', ['new', 'seen', 'applied']);
  }, { read: true });
  R.route('POST /api/tower/insights/:iid/apply', function (ctx) {
    return insightOp(ctx, 'applied', ['new', 'seen']);
  }, { read: true });

  self.DemoTower = { keyFor: keyFor, FALLBACK: FALLBACK, ANSWERS: ANSWERS };
})(self);
