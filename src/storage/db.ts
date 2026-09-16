const DB_NAME = 'livenote-db'
const DB_VERSION = 2

export const STORE_NAMES = {
  sessions: 'sessions',
  segments: 'segments',
  chunks: 'chunks',
  markers: 'markers',
  lifecycleEvents: 'lifecycleEvents',
} as const

let databasePromise: Promise<IDBDatabase> | null = null

export function openDatabase(): Promise<IDBDatabase> {
  if (databasePromise) return databasePromise

  databasePromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION)

    request.onupgradeneeded = (event) => {
      const database = request.result
      const oldVersion = (event as IDBVersionChangeEvent).oldVersion

      if (!database.objectStoreNames.contains(STORE_NAMES.sessions)) {
        const store = database.createObjectStore(STORE_NAMES.sessions, { keyPath: 'id' })
        store.createIndex('status', 'status', { unique: false })
        store.createIndex('updatedAt', 'updatedAt', { unique: false })
      }

      if (!database.objectStoreNames.contains(STORE_NAMES.segments)) {
        const store = database.createObjectStore(STORE_NAMES.segments, { keyPath: 'id' })
        store.createIndex('sessionId', 'sessionId', { unique: false })
        store.createIndex('sessionIndex', ['sessionId', 'index'], { unique: false })
        store.createIndex('status', 'status', { unique: false })
      }

      if (!database.objectStoreNames.contains(STORE_NAMES.chunks)) {
        const store = database.createObjectStore(STORE_NAMES.chunks, { keyPath: 'id' })
        store.createIndex('sessionId', 'sessionId', { unique: false })
        store.createIndex('segmentId', 'segmentId', { unique: false })
        store.createIndex('segmentIndex', ['segmentId', 'index'], { unique: false })
        store.createIndex('uploadStatus', 'uploadStatus', { unique: false })
      }

      if (!database.objectStoreNames.contains(STORE_NAMES.markers)) {
        const store = database.createObjectStore(STORE_NAMES.markers, { keyPath: 'id' })
        store.createIndex('sessionId', 'sessionId', { unique: false })
        store.createIndex('uploadStatus', 'uploadStatus', { unique: false })
      }

      if (!database.objectStoreNames.contains(STORE_NAMES.lifecycleEvents)) {
        const store = database.createObjectStore(STORE_NAMES.lifecycleEvents, { keyPath: 'id' })
        store.createIndex('sessionId', 'sessionId', { unique: false })
        store.createIndex('wallClockMs', 'wallClockMs', { unique: false })
      }

      if (oldVersion < 2 && request.transaction) {
        const chunks = request.transaction.objectStore(STORE_NAMES.chunks)
        const cursorRequest = chunks.openCursor()
        cursorRequest.onsuccess = () => {
          const cursor = cursorRequest.result
          if (!cursor) return
          const value = cursor.value as Record<string, unknown>
          cursor.update({
            ...value,
            sha256: typeof value.sha256 === 'string' ? value.sha256 : '',
            retryCount: typeof value.retryCount === 'number' ? value.retryCount : 0,
            lastUploadAttemptAt: typeof value.lastUploadAttemptAt === 'number' ? value.lastUploadAttemptAt : null,
            uploadedAt: typeof value.uploadedAt === 'number' ? value.uploadedAt : null,
          })
          cursor.continue()
        }
      }
    }

    request.onsuccess = () => {
      const database = request.result
      database.onversionchange = () => database.close()
      resolve(database)
    }
    request.onerror = () => reject(request.error ?? new Error('无法打开 IndexedDB。'))
    request.onblocked = () => reject(new Error('IndexedDB 正在被其他页面占用。'))
  })

  return databasePromise
}

export function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error ?? new Error('IndexedDB 请求失败。'))
  })
}

export function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve()
    transaction.onerror = () => reject(transaction.error ?? new Error('IndexedDB 事务失败。'))
    transaction.onabort = () => reject(transaction.error ?? new Error('IndexedDB 事务被中止。'))
  })
}

export async function putRecord<T extends { id: string }>(storeName: string, record: T): Promise<void> {
  const database = await openDatabase()
  const transaction = database.transaction(storeName, 'readwrite')
  const persistableRecord = storeName === STORE_NAMES.segments
    ? { ...record, mediaSettings: { ...((record as T & { mediaSettings?: Record<string, unknown> }).mediaSettings ?? {}) } }
    : { ...record }
  transaction.objectStore(storeName).put(persistableRecord)
  await transactionDone(transaction)
}

export async function getRecord<T>(storeName: string, id: string): Promise<T | undefined> {
  const database = await openDatabase()
  const transaction = database.transaction(storeName, 'readonly')
  const done = transactionDone(transaction)
  const result = await requestResult(transaction.objectStore(storeName).get(id))
  await done
  return result as T | undefined
}

export async function getAllRecords<T>(storeName: string): Promise<T[]> {
  const database = await openDatabase()
  const transaction = database.transaction(storeName, 'readonly')
  const done = transactionDone(transaction)
  const result = await requestResult(transaction.objectStore(storeName).getAll())
  await done
  return result as T[]
}

export async function getAllByIndex<T>(storeName: string, indexName: string, query: IDBKeyRange | IDBValidKey): Promise<T[]> {
  const database = await openDatabase()
  const transaction = database.transaction(storeName, 'readonly')
  const done = transactionDone(transaction)
  const result = await requestResult(transaction.objectStore(storeName).index(indexName).getAll(query))
  await done
  return result as T[]
}

export async function deleteRecord(storeName: string, id: string): Promise<void> {
  const database = await openDatabase()
  const transaction = database.transaction(storeName, 'readwrite')
  transaction.objectStore(storeName).delete(id)
  await transactionDone(transaction)
}

export async function clearAllData(): Promise<void> {
  const database = await openDatabase()
  const storeNames = Object.values(STORE_NAMES)
  const transaction = database.transaction(storeNames, 'readwrite')
  for (const storeName of storeNames) transaction.objectStore(storeName).clear()
  await transactionDone(transaction)
}
