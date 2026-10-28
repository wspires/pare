package com.example.core;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

class PricingTest {

    @ParameterizedTest
    @CsvSource({"100.0, 0, 100.0", "100.0, 10, 90.0", "80.0, 25, 60.0", "19.99, 50, 9.995"})
    void appliesPercentDiscount(double amount, int percent, double expected) {
        assertEquals(expected, Pricing.applyDiscount(amount, percent));
    }

    @Test
    void rejectsPercentOver100() {
        assertThrows(IllegalArgumentException.class, () -> Pricing.applyDiscount(10, 101));
    }

    @Test
    void formatsTwoDecimals() {
        assertEquals("$12.50", Pricing.format(12.5));
    }

    @Test
    void formatsThousandsSeparator() {
        assertEquals("$1,250.00", Pricing.format(1250));
    }
}
