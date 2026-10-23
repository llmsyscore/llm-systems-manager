// DemoFleet: the sample machines (hardware, models, temperature range) and the demo users.
(function (self) {
  var machines = [
    { id: 'atlas', hostname: 'atlas.example', os: 'linux', ip: '192.0.2.11',
      gpus: [{ name: 'NVIDIA GeForce RTX 4090', vramGb: 24 }, { name: 'NVIDIA GeForce RTX 4090', vramGb: 24 }],
      providers: ['llama', 'vllm'],
      models: ['Llama-3.1-8B-Instruct-Q8_0', 'Qwen2.5-32B-Instruct-Q4_K_M', 'Mistral-7B-Instruct-v0.3'],
      powerW: { idle: 120, load: 780 }, ramGb: 128, diskGb: 2000, diskPct: 46 },
    { id: 'forge', hostname: 'forge.example', os: 'linux', ip: '192.0.2.12',
      gpus: [{ name: 'NVIDIA GeForce RTX 3090', vramGb: 24 }],
      providers: ['llama'],
      models: ['Qwen2.5-14B-Instruct-Q5_K_M', 'Gemma-2-9B-it-Q6_K'],
      powerW: { idle: 70, load: 420 }, ramGb: 64, diskGb: 1000, diskPct: 58,
      tempC: { min: 46, max: 84, busy: 0.78, busyWobble: 0.2 } },
    { id: 'studio', hostname: 'studio.example', os: 'macos', ip: '192.0.2.13',
      gpus: [{ name: 'Apple M2 Ultra', vramGb: 128 }],
      providers: ['lms'],
      models: ['Llama-3.3-70B-Instruct-4bit', 'Qwen2.5-Coder-32B-Instruct-8bit'],
      powerW: { idle: 15, load: 160 }, ramGb: 192, diskGb: 2000, diskPct: 37 },
    { id: 'quill', hostname: 'quill.example', os: 'linux', ip: '192.0.2.14',
      gpus: [],
      providers: ['llama'],
      models: ['Phi-3.5-mini-instruct-Q4_K_M'],
      powerW: { idle: 25, load: 95 }, ramGb: 32, diskGb: 500, diskPct: 41 }
  ];

  self.DemoFleet = {
    machines: machines,
    byId: function (id) {
      for (var i = 0; i < machines.length; i++) if (machines[i].id === id) return machines[i];
      return null;
    },
    users: [{ username: 'alex', role: 'admin' }, { username: 'sam', role: 'operator' }]
  };
})(self);
