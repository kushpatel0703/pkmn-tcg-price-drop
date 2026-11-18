// Run on app.dextcg.com (signed in), after the page has loaded and synced.
// Reads the owned-card list and the user's set list that Dex keeps in the
// browser, builds one compact text payload in window.__dexP, and returns its
// length and hash so the copy can be checked. Read-only: changes nothing in Dex.
const openDb = (n) => new Promise((res, rej) => { const r = indexedDB.open(n); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
const getAll = (db, store) => new Promise((res) => { const q = db.transaction(store, "readonly").objectStore(store).getAll(); q.onsuccess = () => res(q.result); });
const owned = await getAll(await openDb("dex"), "ownedCards");
const ab = { normal: "n", reverseHolo: "r", holo: "h" };
const bySet = {};
for (const c of owned) {
  const i = c.cardId.lastIndexOf("-"), s = c.cardId.slice(0, i), n = c.cardId.slice(i + 1);
  const code = Object.entries(c.quantities || {}).filter(([k, v]) => v > 0 && ab[k]).map(([k]) => ab[k]).sort().join("");
  if (code) (bySet[s] = bySet[s] || []).push(n + code);
}
const rqDb = await openDb("react-query");
const raw = await new Promise((res) => { const q = rqDb.transaction("entries").objectStore("entries").get("REACT_QUERY_OFFLINE_CACHE"); q.onsuccess = () => res(q.result); });
const cache = typeof raw === "string" ? JSON.parse(raw) : (raw || {});
const sets = {};
for (const q of ((cache.clientState || cache).queries || [])) {
  if (!JSON.stringify(q.queryKey).includes('"user/sets"')) continue;
  const res = q.state && q.state.data && q.state.data.result;
  if (Array.isArray(res)) for (const s of res) if (s && s.setId) sets[s.setId] = [s.setId, s.name, s.playId || "", s.type || "", s.numberOfOwnedCards ?? "", s.numberOfCards ?? ""].join("|");
}
window.__dexP = Object.values(sets).sort().join("\n") + "\n#\n" +
  Object.entries(bySet).sort().map(([s, a]) => s + "=" + a.sort((x, y) => parseInt(x.replace(/\D/g, "")) - parseInt(y.replace(/\D/g, "")) || x.localeCompare(y)).join(",")).join("|");
let h = 0; for (const ch of window.__dexP) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
({ length: window.__dexP.length, hash: h, owned_entries: owned.length, sets_with_cards: Object.keys(bySet).length, set_details: Object.keys(sets).length });
