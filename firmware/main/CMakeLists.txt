idf_component_register(SRCS "main.c" PRIV_REQUIRES
    modbus meter telemetry mqtt esp_event esp_netif esp_timer nvs_flash)
