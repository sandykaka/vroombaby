from django.shortcuts import render


def marketing(request):
    return render(request, 'castmute/marketing.html')


def support(request):
    return render(request, 'castmute/support.html')


def privacy(request):
    return render(request, 'castmute/privacy.html')
