long long results[3];
int counter(){ static int c=0; c++; return c; }
int main(){ results[0]=counter();results[1]=counter();results[2]=counter(); return 1; } /*1,2,3*/
