struct Pt{int x;int y;}; struct Pt pts[4]={{1,2},{3,4},{5,6},{7,8}}; long long results[3];
int dist2(struct Pt *p){ return p->x*p->x + p->y*p->y; }
int main(){
    struct Pt *p=pts; int i,best=0,bi=0;
    for(i=0;i<4;i++){ int d=dist2(&pts[i]); if(d>best){best=d;bi=i;} }
    results[0]=best; results[1]=bi; results[2]=dist2(p+1);  /* 113,3,25 */
    return 1;
}
