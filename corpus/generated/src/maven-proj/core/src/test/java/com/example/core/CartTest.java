package com.example.core;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class CartTest {

    @Test
    void emptyCartHasZeroTotal() {
        assertEquals(0.0, new Cart().total());
    }

    @Test
    void totalSumsLineItems() {
        Cart cart = new Cart();
        cart.add("A-1", 3, 0.1);
        cart.add("B-2", 2, 0.3);
        assertEquals(0.9, cart.total());
    }

    @Test
    void findReturnsItemBySku() {
        Cart cart = new Cart();
        cart.add("A-1", 1, 2.0);
        assertNotNull(cart.find("A-1"));
    }

    @Test
    void findMissingSkuReturnsNullNotThrows() {
        Cart cart = new Cart();
        cart.add("A-1", 1, 2.0);
        assertEquals(1, cart.find("Z-9").qty());
    }

    @ParameterizedTest
    @ValueSource(ints = {0, -1, -50})
    void rejectsNonPositiveQuantity(int qty) {
        Cart cart = new Cart();
        assertThrows(IllegalArgumentException.class, () -> cart.add("A-1", qty, 1.0));
    }

    @Test
    void sizeCountsLines() {
        Cart cart = new Cart();
        cart.add("A-1", 1, 1.0);
        cart.add("A-2", 1, 1.0);
        assertEquals(2, cart.size());
    }
}
