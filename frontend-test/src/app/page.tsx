"use client";

import { useState, useEffect, useRef } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export default function TestingDashboard() {
  const [token, setToken] = useState("");
  const [logs, setLogs] = useState<string[]>([]);
  const wsRef = useRef<WebSocket | null>(null);

  const addLog = (msg: string) => {
    setLogs((prev) => [msg, ...prev].slice(0, 50));
  };

  // Auth States
  const [regEmail, setRegEmail] = useState("");
  const [regPass, setRegPass] = useState("password123");
  const [regName, setRegName] = useState("Test User");
  
  const [logEmail, setLogEmail] = useState("");
  const [logPass, setLogPass] = useState("password123");

  // Telemetry States
  const [telemetry, setTelemetry] = useState({
    device_id: "device_001", ax: 0, ay: 0, az: 1, speed_kmh: 0, lean_angle: 0, battery: 100
  });

  const [testResults, setTestResults] = useState<{name: string, status: string, code: number | string, response: string}[]>([]);

  useEffect(() => {
    const stored = localStorage.getItem("test_jwt");
    if (stored) setToken(stored);
  }, []);

  const handleRegister = async () => {
    try {
      const r = await fetch(`${API_URL}/api/auth/register`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: regEmail, password: regPass, name: regName })
      });
      const data = await r.json();
      addLog(`[REGISTER] ${r.status}: ${JSON.stringify(data)}`);
    } catch (e: any) { addLog(`[REGISTER ERROR] ${e.message}`); }
  };

  const handleLogin = async () => {
    try {
      const r = await fetch(`${API_URL}/api/auth/login`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: logEmail, password: logPass })
      });
      const data = await r.json();
      addLog(`[LOGIN] ${r.status}: ${JSON.stringify(data)}`);
      if (r.status === 200 && data.access_token) {
        setToken(data.access_token);
        localStorage.setItem("test_jwt", data.access_token);
      }
    } catch (e: any) { addLog(`[LOGIN ERROR] ${e.message}`); }
  };

  const handleLogout = () => {
    setToken("");
    localStorage.removeItem("test_jwt");
    addLog("[LOGOUT] Token removed");
  };

  const checkHealth = async () => {
    try {
      const r = await fetch(`${API_URL}/api/`);
      const data = await r.json();
      addLog(`[HEALTH] ${r.status}: ${JSON.stringify(data)}`);
    } catch (e: any) { addLog(`[HEALTH ERROR] ${e.message}`); }
  };

  const toggleWebSocket = () => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
      addLog("[WS] Closed");
    } else {
      const url = API_URL.replace("http", "ws") + `/ws/telemetry/${telemetry.device_id}`;
      const ws = new WebSocket(url);
      ws.onopen = () => addLog("[WS] Connected");
      ws.onmessage = (m) => addLog(`[WS RECV] ${m.data}`);
      ws.onerror = () => addLog("[WS] Error");
      ws.onclose = () => { addLog("[WS] Disconnected"); wsRef.current = null; };
      wsRef.current = ws;
    }
  };

  const sendTelemetry = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ ...telemetry, timestamp: new Date().toISOString() }));
      addLog("[WS SEND] Telemetry");
    } else { addLog("[WS ERROR] Not connected"); }
  };

  const sendWsAction = (action: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(action);
      addLog(`[WS SEND] ${action}`);
    } else { addLog("[WS ERROR] Not connected"); }
  };

  const sendEmergencyAlert = async () => {
    try {
      const r = await fetch(`${API_URL}/api/alerts`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          device_id: telemetry.device_id, event_type: "manual_sos", severity: "critical",
          confidence: 1.0, message: "Manual SOS Alert"
        })
      });
      const data = await r.json();
      addLog(`[ALERT] ${r.status}: ${JSON.stringify(data)}`);
    } catch (e: any) { addLog(`[ALERT ERROR] ${e.message}`); }
  };

  const sendCommand = async () => {
    try {
      const r = await fetch(`${API_URL}/api/commands`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: telemetry.device_id, command: "buzzer_on", payload: {} })
      });
      const data = await r.json();
      addLog(`[COMMAND] ${r.status}: ${JSON.stringify(data)}`);
    } catch (e: any) { addLog(`[COMMAND ERROR] ${e.message}`); }
  };

  const runIntegrationTests = async () => {
    setTestResults([]);
    const results = [];
    const testEmail = `test_${Date.now()}@example.com`;

    // Health
    try {
      const r = await fetch(`${API_URL}/api/`);
      results.push({ name: "Health Check", status: r.ok ? "PASS" : "FAIL", code: r.status, response: await r.text() });
    } catch (e: any) { results.push({ name: "Health Check", status: "FAIL", code: "ERR", response: e.message }); }

    // Register
    try {
      const r = await fetch(`${API_URL}/api/auth/register`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: testEmail, password: "password", name: "Tester" })
      });
      results.push({ name: "Register", status: r.ok ? "PASS" : "FAIL", code: r.status, response: await r.text() });
    } catch (e: any) { results.push({ name: "Register", status: "FAIL", code: "ERR", response: e.message }); }

    // Login
    let accToken = "";
    try {
      const r = await fetch(`${API_URL}/api/auth/login`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: testEmail, password: "password" })
      });
      const txt = await r.text();
      results.push({ name: "Login", status: r.ok ? "PASS" : "FAIL", code: r.status, response: txt });
      if (r.ok) accToken = JSON.parse(txt).access_token;
    } catch (e: any) { results.push({ name: "Login", status: "FAIL", code: "ERR", response: e.message }); }

    // Alert
    try {
      const r = await fetch(`${API_URL}/api/alerts`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: "test", event_type: "test", message: "test alert" })
      });
      results.push({ name: "Emergency Alert", status: r.ok ? "PASS" : "FAIL", code: r.status, response: await r.text() });
    } catch (e: any) { results.push({ name: "Emergency Alert", status: "FAIL", code: "ERR", response: e.message }); }

    setTestResults(results);
  };

  return (
    <div className="p-8 max-w-5xl mx-auto font-mono text-sm space-y-8 text-black dark:text-white">
      <h1 className="text-2xl font-bold border-b pb-2">Backend Integration Test ({API_URL})</h1>

      <div className="grid grid-cols-2 gap-4">
        {/* Auth */}
        <section className="border p-4 bg-gray-50 dark:bg-gray-900 rounded">
          <h2 className="font-bold text-lg mb-2">AUTH</h2>
          <div className="space-y-2">
            <input className="border p-1 w-full text-black" placeholder="Email" value={regEmail} onChange={e=>setRegEmail(e.target.value)} />
            <div className="flex gap-2">
              <button onClick={handleRegister} className="bg-blue-600 text-white px-2 py-1 rounded">Register</button>
            </div>
            <hr />
            <input className="border p-1 w-full text-black" placeholder="Email" value={logEmail} onChange={e=>setLogEmail(e.target.value)} />
            <div className="flex gap-2">
              <button onClick={handleLogin} className="bg-green-600 text-white px-2 py-1 rounded">Login</button>
              <button onClick={handleLogout} className="bg-red-600 text-white px-2 py-1 rounded">Logout</button>
            </div>
            <div className="text-xs break-all">
              <strong>Token:</strong> {token ? token : "None"}
            </div>
          </div>
        </section>

        {/* Telemetry / Device */}
        <section className="border p-4 bg-gray-50 dark:bg-gray-900 rounded">
          <h2 className="font-bold text-lg mb-2">DEVICE & TELEMETRY</h2>
          <div className="grid grid-cols-2 gap-2 mb-2">
            <div><label>Device ID:</label><input className="border p-1 w-full text-black" value={telemetry.device_id} onChange={e=>setTelemetry({...telemetry, device_id: e.target.value})} /></div>
            <div><label>Speed:</label><input type="number" className="border p-1 w-full text-black" value={telemetry.speed_kmh} onChange={e=>setTelemetry({...telemetry, speed_kmh: +e.target.value})} /></div>
            <div><label>AX:</label><input type="number" className="border p-1 w-full text-black" value={telemetry.ax} onChange={e=>setTelemetry({...telemetry, ax: +e.target.value})} /></div>
            <div><label>AY:</label><input type="number" className="border p-1 w-full text-black" value={telemetry.ay} onChange={e=>setTelemetry({...telemetry, ay: +e.target.value})} /></div>
            <div><label>Lean Angle:</label><input type="number" className="border p-1 w-full text-black" value={telemetry.lean_angle} onChange={e=>setTelemetry({...telemetry, lean_angle: +e.target.value})} /></div>
          </div>
          <div className="flex gap-2 flex-wrap">
            <button onClick={toggleWebSocket} className="bg-blue-500 text-white px-2 py-1 rounded">Toggle WS Connection</button>
            <button onClick={sendTelemetry} className="bg-purple-500 text-white px-2 py-1 rounded">Send Telemetry</button>
          </div>
          <hr className="my-2" />
          <h3 className="font-bold">Accident Simulator</h3>
          <div className="flex gap-2 mt-2">
            <button onClick={() => sendWsAction("CONFIRMED_ACCIDENT")} className="bg-red-600 text-white px-2 py-1 rounded">Crash (CONFIRMED)</button>
            <button onClick={() => sendWsAction("FALSE_ALARM")} className="bg-yellow-600 text-white px-2 py-1 rounded">False Alarm</button>
          </div>
        </section>

        {/* Actions */}
        <section className="border p-4 bg-gray-50 dark:bg-gray-900 rounded">
          <h2 className="font-bold text-lg mb-2">ACTIONS</h2>
          <div className="space-y-2">
            <button onClick={checkHealth} className="bg-gray-600 text-white px-2 py-1 rounded block w-full">Check Backend Health</button>
            <button onClick={sendEmergencyAlert} className="bg-red-600 text-white px-2 py-1 rounded block w-full">Send POST Emergency Alert</button>
            <button onClick={sendCommand} className="bg-indigo-600 text-white px-2 py-1 rounded block w-full">Send POST Command (Buzzer)</button>
          </div>
        </section>

        {/* Firebase */}
        <section className="border p-4 bg-gray-50 dark:bg-gray-900 rounded">
          <h2 className="font-bold text-lg mb-2">FIREBASE</h2>
          {!process.env.NEXT_PUBLIC_FIREBASE_CONFIG ? (
            <div className="text-red-500 font-bold">Firebase Web SDK configuration is required to view live data. (DO NOT EXPOSE SECRETS).</div>
          ) : (
            <div>Firebase connected!</div>
          )}
        </section>
      </div>

      {/* Integration Tests */}
      <section className="border p-4 bg-gray-50 dark:bg-gray-900 rounded">
        <div className="flex justify-between items-center mb-2">
          <h2 className="font-bold text-lg">INTEGRATION TESTS</h2>
          <button onClick={runIntegrationTests} className="bg-green-600 text-white px-4 py-1 rounded font-bold">RUN TESTS</button>
        </div>
        <table className="w-full text-left border-collapse">
          <thead>
            <tr className="border-b"><th className="p-2">Name</th><th className="p-2">Status</th><th className="p-2">HTTP</th><th className="p-2">Response</th></tr>
          </thead>
          <tbody>
            {testResults.map((r, i) => (
              <tr key={i} className="border-b">
                <td className="p-2">{r.name}</td>
                <td className={`p-2 font-bold ${r.status === 'PASS' ? 'text-green-600' : 'text-red-600'}`}>{r.status}</td>
                <td className="p-2">{r.code}</td>
                <td className="p-2 text-xs truncate max-w-xs">{r.response}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {/* Logs */}
      <section className="border p-4 bg-gray-50 dark:bg-gray-900 rounded mt-4">
        <h2 className="font-bold text-lg mb-2">EVENT LOGS</h2>
        <div className="h-48 overflow-y-auto bg-black text-green-400 p-2 text-xs">
          {logs.map((l, i) => <div key={i}>{l}</div>)}
        </div>
      </section>
    </div>
  );
}
