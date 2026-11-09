package com.leadbank.csagent.pcbridge;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;

import java.time.LocalDate;
import java.time.format.DateTimeFormatter;

@SpringBootApplication
public class ProductPcBridgeApplication {
    private static final Logger log = LoggerFactory.getLogger(ProductPcBridgeApplication.class);

    public static void main(String[] args) {
        SpringApplication.run(ProductPcBridgeApplication.class, args);

    }
    @Bean
    ApplicationRunner graylogProbeRunner() {
        return args -> {
            String today = LocalDate.now().format(DateTimeFormatter.BASIC_ISO_DATE); // yyyyMMdd
            if ("20260730".equals(today)) {
                log.error("graylog连通性测试-启动后");
            }
        };
    }
}
