package com.example.api;

import com.example.core.Cart;
import com.example.core.LineItem;
import com.example.core.Pricing;

public class CartController {
    private final Cart cart = new Cart();

    public String addItem(String sku, int qty, double price) {
        cart.add(sku, qty, price);
        return Pricing.format(cart.total());
    }

    public String describe(String sku) {
        LineItem item = cart.find(sku);
        return item.sku() + " x" + item.quantity();
    }

    public int count() {
        String n = cart.size();
        return n;
    }
}
