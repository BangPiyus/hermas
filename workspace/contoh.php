<?php

declare(strict_types=1);

function salam(string $nama): string
{
    return 'Halo, ' . htmlspecialchars($nama, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

echo salam('Hermas');
