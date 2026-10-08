package com.example.core;

import java.util.ArrayList;
import java.util.List;

public class Cart {
    private final List<LineItem> items = new ArrayList<>();

    public void add(String sku, int qty, double unitPrice) {
        if (qty <= 0) {
            throw new IllegalArgumentException("qty must be positive: " + qty);
        }
        items.add(new LineItem(sku, qty, unitPrice));
    }

    public double total() {
        double sum = 0;
        for (LineItem it : items) {
            sum += it.qty() * it.unitPrice();
        }
        return sum;
    }

    public LineItem find(String sku) {
        for (LineItem it : items) {
            if (it.sku().equals(sku)) {
                return it;
            }
        }
        return null;
    }

    public int size() {
        return items.size();
    }
}
