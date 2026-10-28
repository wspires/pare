package com.example.core;

public final class Pricing {
    private Pricing() {
    }

    public static double applyDiscount(double amount, int percent) {
        if (percent < 0 || percent > 100) {
            throw new IllegalArgumentException("percent out of range: " + percent);
        }
        return amount - amount * percent / 100;
    }

    public static String format(double amount) {
        return String.format("$%.2f", amount);
    }
}
