idf_component_register(SRCS "main.c" PRIV_REQUIRES
    modbus meter telemetry logger_mqtt esp_event esp_netif esp_timer nvs_flash)
