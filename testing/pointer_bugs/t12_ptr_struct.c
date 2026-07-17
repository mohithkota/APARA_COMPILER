struct P{int x;int y;}; struct P g={7,11}; long long results[3];
int main(){ struct P *s=&g; results[0]=s->x; results[1]=s->y; s->x=100; results[2]=g.x; return 1; }
