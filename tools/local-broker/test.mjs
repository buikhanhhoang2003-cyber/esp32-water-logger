// Self-test: publish a payload shaped like the logger's telemetry and check it comes back.
//   node test.mjs                       against mqtt://127.0.0.1:1883
//   node test.mjs mqtt://172.16.50.146:1883
// Uses MQTT_USER / MQTT_PASS when the broker requires them.

import mqtt from 'mqtt'

const url = process.argv[2] || 'mqtt://127.0.0.1:1883'
const topic = 'water-logger/readings'
const payload = JSON.stringify({
  building_1: {
    room_1: {
      water: { status: 'ok', total_m3: 12.345, flow_m3h: 0.12 },
      electric: { status: 'ok', voltage_v: 229.6, current_a: 2.31, power_w: 486.2, energy_kwh: 1234.56 }
    }
  }
})

const client = mqtt.connect(url, {
  clientId: `selftest-${process.pid}`,
  username: process.env.MQTT_USER || undefined,
  password: process.env.MQTT_PASS || undefined,
  connectTimeout: 4000,
  reconnectPeriod: 0
})

const fail = (message) => {
  console.error(`THẤT BẠI: ${message}`)
  client.end(true)
  process.exit(1)
}
const timer = setTimeout(() => fail(`không nhận lại được bản tin từ ${url} trong 5 giây`), 5000)

client.on('error', (err) => fail(`${url}: ${err.message}`))
client.on('connect', () => {
  client.subscribe(topic, { qos: 1 }, (err) => {
    if (err) return fail(err.message)
    client.publish(topic, payload, { qos: 1 })
  })
})
client.on('message', (receivedTopic, message) => {
  if (receivedTopic !== topic || message.toString() !== payload) return
  clearTimeout(timer)
  console.log(`OK: broker ${url} nhận và chuyển tiếp bản tin ${message.length} byte trên "${topic}" (QoS 1).`)
  client.end(false, () => process.exit(0))
})
