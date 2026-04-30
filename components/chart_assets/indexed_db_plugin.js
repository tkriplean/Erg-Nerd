/**
 * IndexedDB — generalized async key-value bridge between HyperDiv (Python)
 * and the browser's IndexedDB.
 *
 * Protocol
 *   Python  → JS:  ctx.props.request  = { id, op, store, key?, keys?, value? }
 *   JS      → Python: ctx.updateProp("response", { id, result, error })
 *   JS      → Python: ctx.updateProp("ready", true) when DB is open
 *
 * Operations (op)
 *   "get"        — value or null at (store, key)
 *   "get_many"   — { key: value } for the supplied `keys` (missing keys absent)
 *   "put"        — write (store, key, value)
 *   "put_many"   — write all entries in `value` ({key: value}) in one transaction
 *   "delete"     — delete (store, key)
 *   "get_all"    — list of {key, value} for every record in the store
 *   "keys"       — list of all keys in the store (strings)
 *   "clear"      — empty the store
 *
 * Multiple requests are processed in arrival order (FIFO queue).  The Python
 * dispatcher only writes a new request after seeing the previous response,
 * so in practice the queue is at most one deep.
 */

window.hyperdiv.registerPlugin("IndexedDB", (ctx) => {
  const dbName = ctx.initialProps.db_name || "ergnerd";
  const storeNames = (ctx.initialProps.stores || []).slice();

  let db = null;
  let opening = null;
  let queue = [];
  let processing = false;

  function openDB() {
    if (opening) return opening;
    opening = new Promise((resolve, reject) => {
      const probe = indexedDB.open(dbName);
      probe.onsuccess = () => {
        const existing = probe.result;
        const need = storeNames.filter(
          (s) => !existing.objectStoreNames.contains(s)
        );
        if (need.length === 0) {
          resolve(existing);
          return;
        }
        const newVersion = (existing.version || 1) + 1;
        existing.close();
        const upgrade = indexedDB.open(dbName, newVersion);
        upgrade.onupgradeneeded = () => {
          const udb = upgrade.result;
          for (const s of storeNames) {
            if (!udb.objectStoreNames.contains(s)) {
              udb.createObjectStore(s);
            }
          }
        };
        upgrade.onsuccess = () => resolve(upgrade.result);
        upgrade.onerror = () => reject(upgrade.error);
        upgrade.onblocked = () =>
          reject(new Error("IndexedDB upgrade blocked"));
      };
      probe.onerror = () => reject(probe.error);
    });
    return opening;
  }

  function execute(req) {
    const { op, store, key, keys, value } = req;
    return new Promise((resolve, reject) => {
      if (op === "get") {
        const tx = db.transaction(store, "readonly");
        const r = tx.objectStore(store).get(key);
        r.onsuccess = () =>
          resolve(r.result === undefined ? null : r.result);
        r.onerror = () => reject(r.error);
      } else if (op === "get_many") {
        const tx = db.transaction(store, "readonly");
        const os = tx.objectStore(store);
        const ks = keys || [];
        const out = {};
        if (ks.length === 0) {
          resolve(out);
          return;
        }
        let pending = ks.length;
        for (const k of ks) {
          const r = os.get(k);
          r.onsuccess = () => {
            if (r.result !== undefined) out[k] = r.result;
            if (--pending === 0) resolve(out);
          };
          r.onerror = () => reject(r.error);
        }
      } else if (op === "put") {
        const tx = db.transaction(store, "readwrite");
        tx.objectStore(store).put(value, key);
        tx.oncomplete = () => resolve(true);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      } else if (op === "put_many") {
        const tx = db.transaction(store, "readwrite");
        const os = tx.objectStore(store);
        const entries = value || {};
        for (const k of Object.keys(entries)) {
          os.put(entries[k], k);
        }
        tx.oncomplete = () => resolve(true);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      } else if (op === "delete") {
        const tx = db.transaction(store, "readwrite");
        tx.objectStore(store).delete(key);
        tx.oncomplete = () => resolve(true);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      } else if (op === "get_all") {
        const tx = db.transaction(store, "readonly");
        const os = tx.objectStore(store);
        let gotKeys = null;
        let gotVals = null;
        const tryFinish = () => {
          if (gotKeys !== null && gotVals !== null) {
            const out = [];
            for (let i = 0; i < gotKeys.length; i++) {
              out.push({ key: String(gotKeys[i]), value: gotVals[i] });
            }
            resolve(out);
          }
        };
        const kr = os.getAllKeys();
        kr.onsuccess = () => {
          gotKeys = kr.result;
          tryFinish();
        };
        kr.onerror = () => reject(kr.error);
        const vr = os.getAll();
        vr.onsuccess = () => {
          gotVals = vr.result;
          tryFinish();
        };
        vr.onerror = () => reject(vr.error);
      } else if (op === "keys") {
        const tx = db.transaction(store, "readonly");
        const r = tx.objectStore(store).getAllKeys();
        r.onsuccess = () => resolve(r.result.map(String));
        r.onerror = () => reject(r.error);
      } else if (op === "clear") {
        const tx = db.transaction(store, "readwrite");
        tx.objectStore(store).clear();
        tx.oncomplete = () => resolve(true);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      } else {
        reject(new Error("Unknown op: " + op));
      }
    });
  }

  async function drain() {
    if (processing) return;
    processing = true;
    try {
      while (queue.length > 0) {
        const req = queue.shift();
        let resp;
        try {
          const result = await execute(req);
          resp = { id: req.id, result: result, error: null };
        } catch (err) {
          const msg = err && (err.message || String(err));
          console.error("[IndexedDB] op failed:", req, err);
          resp = { id: req.id, result: null, error: msg || "unknown error" };
        }
        ctx.updateProp("response", resp);
      }
    } finally {
      processing = false;
    }
  }

  function enqueue(req) {
    if (!req || req.id == null || !req.op) return;
    queue.push(req);
    if (db) drain();
  }

  // Open the database; once ready, drain any queued requests.
  openDB()
    .then((d) => {
      db = d;
      ctx.updateProp("ready", true);
      drain();
    })
    .catch((err) => {
      console.error("[IndexedDB] open failed:", err);
      ctx.updateProp(
        "response",
        { id: -1, result: null, error: "open failed: " + err }
      );
    });

  // If a request was already set in initialProps before this plugin loaded,
  // honour it.
  if (ctx.initialProps.request) {
    enqueue(ctx.initialProps.request);
  }

  ctx.onPropUpdate((propName, propValue) => {
    if (propName === "request") {
      enqueue(propValue);
    }
  });
});
