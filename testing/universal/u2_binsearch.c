long long results[4];
int bsearch(int *a,int n,int key){ int lo=0,hi=n-1; while(lo<=hi){int m=(lo+hi)/2; if(a[m]==key)return m; if(a[m]<key)lo=m+1; else hi=m-1;} return -1; }
int main(){
    int a[7]={1,3,5,7,9,11,13};
    results[0]=bsearch(a,7,7); results[1]=bsearch(a,7,1);
    results[2]=bsearch(a,7,13); results[3]=bsearch(a,7,8);  /* 3,0,6,-1 */
    return 1;
}
