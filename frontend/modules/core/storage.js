export function storageGet(storage, key, fallback = null) {
  try {
    return storage.getItem(key) ?? fallback;
  } catch (_) {
    return fallback;
  }
}

export function storageSet(storage, key, value) {
  try {
    storage.setItem(key, value);
    return true;
  } catch (_) {
    return false;
  }
}

export function storageJsonGet(storage, key, fallback) {
  const value = storageGet(storage, key);
  if (value == null) return fallback;
  try {
    return JSON.parse(value);
  } catch (_) {
    return fallback;
  }
}

export function storageJsonSet(storage, key, value) {
  try {
    return storageSet(storage, key, JSON.stringify(value));
  } catch (_) {
    return false;
  }
}
